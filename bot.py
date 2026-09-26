import asyncio
import aiosqlite
import random
import logging
from datetime import datetime, timezone, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO)

TOKEN = "8491747741:AAH09WqmWxSwCpKJLdVIU42ScVLX5b2Uje8"
ADMINS = [5679778859]
DB = "bot.db"
MSK = timezone(timedelta(hours=3))

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ── FSM ──────────────────────────────────────────────────

class New(StatesGroup):
    title       = State()
    description = State()
    winners     = State()
    btn_text    = State()
    channel     = State()
    pub_time    = State()
    end_time    = State()
    req_subs    = State()   # выбор каналов обязательной подписки
    preview     = State()   # предпросмотр перед созданием

class Edit(StatesGroup):
    field = State()
    value = State()
    cid   = State()

class AddChannel(StatesGroup):
    waiting = State()

# ── DATABASE ─────────────────────────────────────────────

async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS contests (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id       INTEGER,
                title          TEXT,
                description    TEXT,
                photo_id       TEXT,
                video_id       TEXT,
                winners_count  INTEGER DEFAULT 1,
                btn_text       TEXT DEFAULT '🎟 Участвовать',
                channel_id     TEXT,
                channel_title  TEXT,
                pub_time       TEXT,
                end_time       TEXT,
                message_id     INTEGER,
                status         TEXT DEFAULT 'draft',
                require_sub    INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id  INTEGER,
                user_id     INTEGER,
                name        TEXT,
                username    TEXT,
                UNIQUE(contest_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id    INTEGER,
                channel_id  TEXT,
                title       TEXT,
                UNIQUE(owner_id, channel_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS required_channels (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                contest_id  INTEGER,
                channel_id  TEXT,
                title       TEXT,
                UNIQUE(contest_id, channel_id)
            )
        """)
        # ── миграция: добавляем колонки которых может не быть в старой БД ──
        migrations = [
            # contests
            ("contests", "photo_id",      "TEXT"),
            ("contests", "video_id",      "TEXT"),
            ("contests", "winners_count", "INTEGER DEFAULT 1"),
            ("contests", "btn_text",      "TEXT DEFAULT '🎟 Участвовать'"),
            ("contests", "channel_title", "TEXT"),
            ("contests", "pub_time",      "TEXT"),
            ("contests", "end_time",      "TEXT"),
            ("contests", "message_id",    "INTEGER"),
            ("contests", "require_sub",   "INTEGER DEFAULT 0"),
            # participants
            ("participants", "username",  "TEXT"),
        ]
        for table, col, col_def in migrations:
            try:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
                logging.info(f"Migration: added {table}.{col}")
            except Exception:
                pass  # колонка уже есть — нормально

        await db.commit()

async def db_get(q, a=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, a)
        return await cur.fetchall()

async def db_one(q, a=()):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, a)
        return await cur.fetchone()

async def db_run(q, a=()):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(q, a)
        await db.commit()
        return cur.lastrowid

# ── TIME HELPERS ─────────────────────────────────────────

def now_msk():
    return datetime.now(MSK)

def parse_time(s):
    s = s.strip()
    try:
        if " " in s:
            dt = datetime.strptime(s, "%d.%m %H:%M")
            dt = dt.replace(year=now_msk().year, tzinfo=MSK)
        else:
            dt = datetime.strptime(s, "%H:%M")
            dt = now_msk().replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        return dt
    except Exception:
        return None

def fmt_time(s):
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%d.%m.%Y %H:%M МСК")
    except Exception:
        return s

# ── KEYBOARDS ────────────────────────────────────────────

def main_kb(uid):
    rows = [
        [KeyboardButton(text="🎲 Создать конкурс"), KeyboardButton(text="📋 Мои конкурсы")],
        [KeyboardButton(text="📢 Мои каналы"),       KeyboardButton(text="ℹ️ Помощь")]
    ]
    if uid in ADMINS:
        rows.append([KeyboardButton(text="⚙️ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True)

def skip_cancel_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отмена")]
    ], resize_keyboard=True)

def contest_kb(cid, is_owner, status):
    btns = []
    if is_owner:
        if status == "draft":
            btns.append([InlineKeyboardButton(text="📢 Опубликовать сейчас", callback_data=f"pubnow:{cid}")])
        btns.append([
            InlineKeyboardButton(text="✏️ Изменить",  callback_data=f"edit:{cid}"),
            InlineKeyboardButton(text="🏆 Розыгрыш", callback_data=f"draw:{cid}")
        ])
        btns.append([
            InlineKeyboardButton(text="👥 Участники", callback_data=f"plist:{cid}"),
            InlineKeyboardButton(text="🗑 Удалить",   callback_data=f"del:{cid}")
        ])
    else:
        btns.append([InlineKeyboardButton(text="👥 Участники", callback_data=f"plist:{cid}")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def join_kb(cid, btn_text, bot_username=""):
    if bot_username:
        url = f"https://t.me/{bot_username}?start=join_{cid}"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_text, url=url)]
        ])
    # fallback — если username ещё не получен, используем callback
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, callback_data=f"join:{cid}")]
    ])

def edit_kb(cid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Название",    callback_data=f"ef:title:{cid}"),
         InlineKeyboardButton(text="📄 Описание",    callback_data=f"ef:desc:{cid}")],
        [InlineKeyboardButton(text="👑 Победители",  callback_data=f"ef:winners_count:{cid}"),
         InlineKeyboardButton(text="🎟 Кнопка",      callback_data=f"ef:btn_text:{cid}")],
        [InlineKeyboardButton(text="📢 Канал",       callback_data=f"ef:channel:{cid}"),
         InlineKeyboardButton(text="⏰ Время публ.", callback_data=f"ef:pub_time:{cid}")],
        [InlineKeyboardButton(text="🏁 Время оконч.", callback_data=f"ef:end_time:{cid}"),
         InlineKeyboardButton(text="🔒 Подписка",    callback_data=f"ef:req_sub:{cid}")],
        [InlineKeyboardButton(text="⬅️ Назад",       callback_data=f"open:{cid}")]
    ])

def confirm_kb(cid, action):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да",  callback_data=f"{action}_yes:{cid}"),
         InlineKeyboardButton(text="❌ Нет", callback_data=f"open:{cid}")]
    ])

def list_kb(rows, prefix="open"):
    kb = []
    for c in rows:
        icon = {"draft": "📝", "active": "✅", "finished": "🔴"}.get(c["status"], "❓")
        kb.append([InlineKeyboardButton(
            text=f"{icon} {c['title'][:35]}",
            callback_data=f"{prefix}:{c['id']}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def channels_kb(channels, prefix="selchan"):
    kb = []
    for ch in channels:
        kb.append([InlineKeyboardButton(text=ch["title"], callback_data=f"{prefix}:{ch['channel_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def req_subs_kb(channels, selected_ids):
    """Клавиатура мультивыбора каналов для обязательной подписки"""
    kb = []
    for ch in channels:
        mark = "✅" if ch["channel_id"] in selected_ids else "☑️"
        kb.append([InlineKeyboardButton(
            text=f"{mark} {ch['title']}",
            callback_data=f"rsc_toggle:{ch['channel_id']}"
        )])
    kb.append([
        InlineKeyboardButton(text="✅ Готово",       callback_data="rsc_done"),
        InlineKeyboardButton(text="⏭ Пропустить",   callback_data="rsc_skip")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ── HELPERS ──────────────────────────────────────────────

async def contest_text(c):
    count = await db_one("SELECT COUNT(*) as n FROM participants WHERE contest_id=?", (c["id"],))
    n = count["n"] if count else 0
    status_map = {"draft": "📝 Черновик", "active": "✅ Активен", "finished": "🔴 Завершён"}
    lines = [f"🎲 <b>{c['title']}</b>"]
    if c["description"]:
        lines.append(f"\n{c['description']}")
    lines.append(f"\n👑 Победителей: <b>{c['winners_count']}</b>")
    lines.append(f"👥 Участников: <b>{n}</b>")
    lines.append(f"📊 Статус: {status_map.get(c['status'], c['status'])}")
    if c["channel_title"]:
        lines.append(f"📢 Канал публикации: {c['channel_title']}")
    if c["pub_time"]:
        lines.append(f"⏰ Публикация: {fmt_time(c['pub_time'])}")
    if c["end_time"]:
        lines.append(f"🏁 Окончание: {fmt_time(c['end_time'])}")
    lines.append(f"🎟 Кнопка: «{c['btn_text']}»")
    if c["require_sub"]:
        req = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (c["id"],))
        if req:
            ch_list = ", ".join(r["title"] for r in req)
            lines.append(f"🔒 Обязательная подписка: {ch_list}")
    return "\n".join(lines)

async def public_post_text(c):
    lines = [f"🎲 <b>{c['title']}</b>"]
    if c["description"]:
        lines.append(f"\n{c['description']}")
    lines.append(f"\n👑 Победителей: <b>{c['winners_count']}</b>")
    if c["end_time"]:
        lines.append(f"🏁 Окончание: {fmt_time(c['end_time'])}")
    if c["require_sub"]:
        req = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (c["id"],))
        if req:
            ch_list = "\n".join(f"  • {r['title']}" for r in req)
            lines.append(f"\n🔒 Для участия подпишитесь на:\n{ch_list}")
    return "\n".join(lines)

async def check_subscriptions(bot: Bot, user_id: int, contest_id: int) -> list:
    """Возвращает список названий каналов, на которые пользователь НЕ подписан"""
    req = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (contest_id,))
    not_subbed = []
    for ch in req:
        try:
            member = await bot.get_chat_member(ch["channel_id"], user_id)
            if member.status in ("left", "kicked", "banned"):
                not_subbed.append(ch["title"])
        except Exception:
            not_subbed.append(ch["title"])
    return not_subbed

async def publish_contest(bot: Bot, c):
    """Публикует конкурс в канал, возвращает message_id"""
    cid     = c["id"]
    text    = await public_post_text(c)
    channel = c["channel_id"]
    try:
        me  = await bot.get_me()
        kb  = join_kb(cid, c["btn_text"], me.username)
        if c["photo_id"]:
            msg = await bot.send_photo(channel, c["photo_id"], caption=text, parse_mode="HTML", reply_markup=kb)
        elif c["video_id"]:
            msg = await bot.send_video(channel, c["video_id"], caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            msg = await bot.send_message(channel, text, parse_mode="HTML", reply_markup=kb)
        return msg.message_id
    except Exception as e:
        logging.error(f"Publish error: {e}")
        return None

async def send_preview(target: Message, state: FSMContext):
    """Отправляет предпросмотр поста с кнопками подтверждения"""
    data         = await state.get_data()
    req_channels = data.get("req_sub_channels", [])

    lines = [f"🎲 <b>{data['title']}</b>"]
    if data.get("description"):
        lines.append(f"\n{data['description']}")
    lines.append(f"\n👑 Победителей: <b>{data['winners']}</b>")
    if data.get("end_time"):
        lines.append(f"🏁 Окончание: {fmt_time(data['end_time'])}")
    if req_channels:
        ch_list = "\n".join(f"  • {ch['title']}" for ch in req_channels)
        lines.append(f"\n🔒 Для участия подпишитесь на:\n{ch_list}")

    preview_text = "\n".join(lines)
    btn_text     = data.get("btn_text", "🎟 Участвовать")

    confirm_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_text, callback_data="preview_dummy")],
        [InlineKeyboardButton(text="✅ Создать конкурс", callback_data="new_confirm"),
         InlineKeyboardButton(text="❌ Отмена",          callback_data="new_cancel")]
    ])

    await target.answer("👁 <b>Предпросмотр поста в канале:</b>", parse_mode="HTML")

    if data.get("photo_id"):
        await target.answer_photo(data["photo_id"], caption=preview_text,
                                  parse_mode="HTML", reply_markup=confirm_markup)
    elif data.get("video_id"):
        await target.answer_video(data["video_id"], caption=preview_text,
                                  parse_mode="HTML", reply_markup=confirm_markup)
    else:
        await target.answer(preview_text, parse_mode="HTML", reply_markup=confirm_markup)

# ── START / HELP ──────────────────────────────────────────

@dp.message(CommandStart())
async def start(msg: Message):
    # Deep-link: /start join_<contest_id>
    payload = msg.text.split(maxsplit=1)[1] if len(msg.text.split()) > 1 else ""
    if payload.startswith("join_"):
        try:
            cid = int(payload[5:])
        except ValueError:
            cid = None

        if cid:
            c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
            if c and c["status"] == "active":
                # Проверка времени
                if c["end_time"]:
                    end_dt = datetime.fromisoformat(c["end_time"])
                    if now_msk() > end_dt:
                        await msg.answer(
                            "⏰ Время участия в конкурсе истекло.",
                            reply_markup=main_kb(msg.from_user.id)
                        )
                        return

                # Проверка подписки
                if c["require_sub"]:
                    not_subbed = await check_subscriptions(msg.bot, msg.from_user.id, cid)
                    if not_subbed:
                        ch_list = "\n".join(f"• {t}" for t in not_subbed)
                        await msg.answer(
                            f"❌ Для участия сначала подпишитесь на:\n{ch_list}",
                            parse_mode="HTML",
                            reply_markup=main_kb(msg.from_user.id)
                        )
                        return

                # Регистрируем участника
                await db_run(
                    "INSERT OR IGNORE INTO participants (contest_id, user_id, name, username) VALUES (?,?,?,?)",
                    (cid, msg.from_user.id, msg.from_user.full_name, msg.from_user.username)
                )

                # Формируем ссылку на пост конкурса в канале
                channel_link = ""
                if c["channel_id"] and c["message_id"]:
                    chan_id_clean = str(c["channel_id"]).replace("-100", "")
                    channel_link = f"https://t.me/c/{chan_id_clean}/{c['message_id']}"

                if channel_link:
                    contest_href = f'<a href="{channel_link}">конкурса</a>'
                else:
                    contest_href = "конкурса"

                await msg.answer(
                    f"🎉 Теперь вы участник {contest_href}!\n"
                    "🎁 Ждите итоги.",
                    parse_mode="HTML",
                    reply_markup=main_kb(msg.from_user.id)
                )
                return

    # Обычный старт
    await msg.answer(
        f"👋 <b>Привет, {msg.from_user.full_name}!</b>\n\n"
        "🎲 Бот для честных конкурсов и розыгрышей.\n\n"
        "<b>Начните с добавления канала:</b>\n"
        "📢 Мои каналы → Добавить канал\n\n"
        "Затем создайте конкурс и опубликуйте его 🚀",
        parse_mode="HTML",
        reply_markup=main_kb(msg.from_user.id)
    )

@dp.message(F.text == "ℹ️ Помощь")
@dp.message(Command("help"))
async def help_cmd(msg: Message):
    await msg.answer(
        "📖 <b>Команды:</b>\n\n"
        "/newlot — создать конкурс\n"
        "/mylot — мои конкурсы\n"
        "/dellot — удалить конкурс\n"
        "/editlot — редактировать конкурс\n"
        "/channels — мои каналы\n"
        "/help — помощь\n\n"
        "⚠️ <b>Бот должен быть администратором в канале/группе!</b>",
        parse_mode="HTML"
    )

# ── CHANNELS ─────────────────────────────────────────────

@dp.message(F.text == "📢 Мои каналы")
@dp.message(Command("channels"))
async def my_channels(msg: Message):
    rows = await db_get("SELECT * FROM channels WHERE owner_id=?", (msg.from_user.id,))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="addchan")]
    ] + [
        [InlineKeyboardButton(text=f"🗑 {r['title']}", callback_data=f"delchan:{r['channel_id']}")]
        for r in rows
    ])
    text = "📢 <b>Мои каналы/группы</b>\n\n"
    if rows:
        text += "\n".join(f"• {r['title']} (<code>{r['channel_id']}</code>)" for r in rows)
    else:
        text += "Каналов нет. Добавьте первый."
    await msg.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data == "addchan")
async def add_channel_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(AddChannel.waiting)
    await call.message.answer(
        "📢 <b>Добавление канала</b>\n\n"
        "1. Добавьте бота в канал/группу как <b>администратора</b>\n"
        "2. Перешлите сюда <b>любое сообщение</b> из этого канала/группы\n"
        "   или введите ID вручную (например: <code>-1001234567890</code>)",
        parse_mode="HTML",
        reply_markup=cancel_kb()
    )
    await call.answer()

@dp.message(AddChannel.waiting)
async def add_channel_receive(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))

    channel_id = None
    title = None

    if msg.forward_from_chat:
        channel_id = str(msg.forward_from_chat.id)
        title = msg.forward_from_chat.title
    elif msg.text and msg.text.startswith("-100"):
        channel_id = msg.text.strip()
        try:
            chat = await msg.bot.get_chat(channel_id)
            title = chat.title
        except Exception:
            return await msg.answer("⚠️ Не удалось найти чат. Убедитесь, что бот добавлен как администратор.")

    if not channel_id:
        return await msg.answer("⚠️ Перешлите сообщение из канала или введите ID.")

    try:
        member = await msg.bot.get_chat_member(channel_id, msg.bot.id)
        if member.status not in ("administrator", "creator"):
            return await msg.answer("⚠️ Бот не является администратором в этом канале!")
    except Exception:
        return await msg.answer("⚠️ Нет доступа к каналу. Добавьте бота как администратора.")

    await db_run(
        "INSERT OR IGNORE INTO channels (owner_id, channel_id, title) VALUES (?,?,?)",
        (msg.from_user.id, channel_id, title)
    )
    await state.clear()
    await msg.answer(f"✅ Канал <b>{title}</b> добавлен!", parse_mode="HTML",
                     reply_markup=main_kb(msg.from_user.id))

@dp.callback_query(F.data.startswith("delchan:"))
async def del_channel(call: CallbackQuery):
    cid = call.data.split(":")[1]
    await db_run("DELETE FROM channels WHERE owner_id=? AND channel_id=?", (call.from_user.id, cid))
    await call.answer("✅ Канал удалён.", show_alert=True)
    rows = await db_get("SELECT * FROM channels WHERE owner_id=?", (call.from_user.id,))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить канал", callback_data="addchan")]
    ] + [
        [InlineKeyboardButton(text=f"🗑 {r['title']}", callback_data=f"delchan:{r['channel_id']}")]
        for r in rows
    ])
    await call.message.edit_text("📢 <b>Мои каналы/группы</b>", parse_mode="HTML", reply_markup=kb)

# ── CREATE CONTEST ────────────────────────────────────────

@dp.message(F.text == "🎲 Создать конкурс")
@dp.message(Command("newlot"))
async def new_start(msg: Message, state: FSMContext):
    await state.set_state(New.title)
    await msg.answer("📝 Введите <b>название</b> конкурса:", parse_mode="HTML", reply_markup=cancel_kb())

@dp.message(New.title)
async def new_title(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))
    await state.update_data(title=msg.text)
    await state.set_state(New.description)
    await msg.answer(
        "📄 Отправьте <b>описание</b> конкурса:\n"
        "Можно текст, фото или видео.\n"
        "Или нажмите <b>Пропустить</b>.",
        parse_mode="HTML",
        reply_markup=skip_cancel_kb()
    )

@dp.message(New.description)
async def new_description(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))

    desc = photo_id = video_id = None
    if msg.text == "⏭ Пропустить":
        pass
    elif msg.photo:
        photo_id = msg.photo[-1].file_id
        desc = msg.caption or None
    elif msg.video:
        video_id = msg.video.file_id
        desc = msg.caption or None
    elif msg.text:
        desc = msg.text

    await state.update_data(description=desc, photo_id=photo_id, video_id=video_id)
    await state.set_state(New.winners)
    await msg.answer("👑 Сколько <b>победителей</b>? (число от 1):", parse_mode="HTML",
                     reply_markup=cancel_kb())

@dp.message(New.winners)
async def new_winners(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))
    if not msg.text.isdigit() or int(msg.text) < 1:
        return await msg.answer("⚠️ Введите число от 1.")
    await state.update_data(winners=int(msg.text))
    await state.set_state(New.btn_text)
    await msg.answer(
        "🎟 Введите <b>текст кнопки</b> для участия\n"
        "Например: «Участвовать», «Хочу участвовать» и т.д.\n\n"
        "Или нажмите <b>Пропустить</b> (будет «🎟 Участвовать»).",
        parse_mode="HTML",
        reply_markup=skip_cancel_kb()
    )

@dp.message(New.btn_text)
async def new_btn_text(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))
    btn = "🎟 Участвовать" if msg.text == "⏭ Пропустить" else msg.text
    await state.update_data(btn_text=btn)
    await state.set_state(New.channel)

    channels = await db_get("SELECT * FROM channels WHERE owner_id=?", (msg.from_user.id,))
    if not channels:
        return await msg.answer(
            "⚠️ У вас нет добавленных каналов.\n"
            "Сначала добавьте канал в разделе 📢 Мои каналы.",
            reply_markup=main_kb(msg.from_user.id)
        )
    await msg.answer("📢 Выберите <b>канал</b> для публикации:", parse_mode="HTML",
                     reply_markup=channels_kb(channels, prefix="selnew"))

@dp.callback_query(New.channel, F.data.startswith("selnew:"))
async def new_channel_selected(call: CallbackQuery, state: FSMContext):
    chan_id = call.data.split(":", 1)[1]
    ch = await db_one("SELECT * FROM channels WHERE channel_id=?", (chan_id,))
    await state.update_data(channel_id=chan_id, channel_title=ch["title"] if ch else chan_id)
    await state.set_state(New.pub_time)
    n = now_msk()
    ex_time = n.strftime("%H:%M")
    ex_date = n.strftime("%d.%m")
    await call.message.answer(
        f"⏰ Введите <b>время публикации</b> (МСК):\n\n"
        f"• Только время (сегодня): <code>{ex_time}</code>\n"
        f"• Дата + время: <code>{ex_date} {ex_time}</code>\n\n"
        "Или нажмите <b>Пропустить</b> (опубликовать сейчас).",
        parse_mode="HTML",
        reply_markup=skip_cancel_kb()
    )
    await call.answer()

@dp.message(New.pub_time)
async def new_pub_time(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))

    pub_time = None
    if msg.text != "⏭ Пропустить":
        dt = parse_time(msg.text)
        if not dt:
            return await msg.answer(
                "⚠️ Неверный формат.\nПримеры: <code>13:09</code> или <code>25.12 13:09</code>",
                parse_mode="HTML"
            )
        pub_time = dt.isoformat()

    await state.update_data(pub_time=pub_time)
    await state.set_state(New.end_time)
    n = now_msk()
    ex_time = n.strftime("%H:%M")
    ex_date = n.strftime("%d.%m")
    await msg.answer(
        f"🏁 Введите <b>время окончания</b> конкурса (МСК):\n\n"
        f"• Только время (сегодня): <code>{ex_time}</code>\n"
        f"• Дата + время: <code>{ex_date} {ex_time}</code>\n\n"
        "Или нажмите <b>Пропустить</b>.",
        parse_mode="HTML",
        reply_markup=skip_cancel_kb()
    )

@dp.message(New.end_time)
async def new_end_time(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))

    end_time = None
    if msg.text != "⏭ Пропустить":
        dt = parse_time(msg.text)
        if not dt:
            return await msg.answer(
                "⚠️ Неверный формат.\nПримеры: <code>13:09</code> или <code>25.12 20:00</code>",
                parse_mode="HTML"
            )
        end_time = dt.isoformat()

    await state.update_data(end_time=end_time, req_sub_channels=[], req_sub_ids=[])
    await state.set_state(New.req_subs)

    channels = await db_get("SELECT * FROM channels WHERE owner_id=?", (msg.from_user.id,))
    if not channels:
        # Нет каналов для выбора — сразу к предпросмотру
        await state.set_state(New.preview)
        return await send_preview(msg, state)

    await msg.answer(
        "🔒 <b>Обязательная подписка</b>\n\n"
        "Выберите каналы, на которые участник должен подписаться перед участием в конкурсе.\n\n"
        "Или нажмите <b>Пропустить</b>.",
        parse_mode="HTML",
        reply_markup=req_subs_kb(channels, [])
    )

# ── REQ SUBS SELECTION ────────────────────────────────────

@dp.callback_query(New.req_subs, F.data.startswith("rsc_toggle:"))
async def rsc_toggle(call: CallbackQuery, state: FSMContext):
    chan_id = call.data.split(":", 1)[1]
    data     = await state.get_data()
    selected = data.get("req_sub_ids", [])
    req_chs  = data.get("req_sub_channels", [])

    if chan_id in selected:
        selected = [x for x in selected if x != chan_id]
        req_chs  = [c for c in req_chs if c["channel_id"] != chan_id]
    else:
        selected.append(chan_id)
        ch = await db_one("SELECT * FROM channels WHERE channel_id=?", (chan_id,))
        if ch:
            req_chs.append({"channel_id": chan_id, "title": ch["title"]})

    await state.update_data(req_sub_ids=selected, req_sub_channels=req_chs)
    channels = await db_get("SELECT * FROM channels WHERE owner_id=?", (call.from_user.id,))
    await call.message.edit_reply_markup(reply_markup=req_subs_kb(channels, selected))
    await call.answer()

@dp.callback_query(New.req_subs, F.data.in_({"rsc_done", "rsc_skip"}))
async def rsc_done(call: CallbackQuery, state: FSMContext):
    if call.data == "rsc_skip":
        await state.update_data(req_sub_channels=[], req_sub_ids=[])
    await state.set_state(New.preview)
    await send_preview(call.message, state)
    await call.answer()

# ── PREVIEW CONFIRM ───────────────────────────────────────

@dp.callback_query(F.data == "preview_dummy")
async def preview_dummy(call: CallbackQuery):
    await call.answer("Это предпросмотр — кнопка не активна.", show_alert=True)

@dp.callback_query(New.preview, F.data == "new_confirm")
async def new_preview_confirm(call: CallbackQuery, state: FSMContext):
    data         = await state.get_data()
    req_channels = data.get("req_sub_channels", [])

    cid = await db_run(
        """INSERT INTO contests
           (owner_id, title, description, photo_id, video_id, winners_count, btn_text,
            channel_id, channel_title, pub_time, end_time, status, require_sub)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            call.from_user.id,
            data["title"],
            data.get("description"),
            data.get("photo_id"),
            data.get("video_id"),
            data["winners"],
            data["btn_text"],
            data.get("channel_id"),
            data.get("channel_title"),
            data.get("pub_time"),
            data.get("end_time"),
            "draft",
            1 if req_channels else 0
        )
    )

    for ch in req_channels:
        await db_run(
            "INSERT OR IGNORE INTO required_channels (contest_id, channel_id, title) VALUES (?,?,?)",
            (cid, ch["channel_id"], ch["title"])
        )

    await state.clear()
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    await call.message.answer("✅ <b>Конкурс создан!</b>", parse_mode="HTML",
                              reply_markup=main_kb(call.from_user.id))

    if not data.get("pub_time"):
        mid = await publish_contest(call.bot, c)
        if mid:
            await db_run("UPDATE contests SET status='active', message_id=? WHERE id=?", (mid, cid))
            await call.message.answer("📢 Конкурс опубликован в канал!",
                                      reply_markup=main_kb(call.from_user.id))
        else:
            await call.message.answer("⚠️ Не удалось опубликовать. Проверьте права бота.")
    else:
        await call.message.answer(
            await contest_text(c), parse_mode="HTML",
            reply_markup=contest_kb(cid, True, "draft")
        )
    await call.answer()

@dp.callback_query(New.preview, F.data == "new_cancel")
async def new_preview_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("❌ Создание конкурса отменено.",
                              reply_markup=main_kb(call.from_user.id))
    await call.answer()

# ── MY LOTS ──────────────────────────────────────────────

@dp.message(F.text == "📋 Мои конкурсы")
@dp.message(Command("mylot"))
async def my_lots(msg: Message):
    rows = await db_get("SELECT * FROM contests WHERE owner_id=? ORDER BY id DESC", (msg.from_user.id,))
    if not rows:
        return await msg.answer("📭 Нет конкурсов. Создайте: /newlot")
    await msg.answer(f"📋 <b>Ваши конкурсы ({len(rows)}):</b>", parse_mode="HTML",
                     reply_markup=list_kb(rows))

@dp.callback_query(F.data.startswith("open:"))
async def open_contest(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    is_owner = c["owner_id"] == call.from_user.id or call.from_user.id in ADMINS
    if not is_owner:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    await call.message.edit_text(
        await contest_text(c), parse_mode="HTML",
        reply_markup=contest_kb(cid, is_owner, c["status"])
    )

# ── PUBLISH NOW ───────────────────────────────────────────

@dp.callback_query(F.data.startswith("pubnow:"))
async def pub_now(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    mid = await publish_contest(call.bot, c)
    if mid:
        await db_run("UPDATE contests SET status='active', message_id=? WHERE id=?", (mid, cid))
        await call.message.edit_text("✅ Конкурс опубликован в канал!")
    else:
        await call.answer("⚠️ Ошибка публикации. Бот должен быть администратором в канале.",
                          show_alert=True)

# ── JOIN ─────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("join:"))
async def join(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c or c["status"] != "active":
        return await call.answer("⛔ Конкурс недоступен или завершён.", show_alert=True)

    if c["end_time"]:
        end_dt = datetime.fromisoformat(c["end_time"])
        if now_msk() > end_dt:
            return await call.answer("⏰ Время участия истекло.", show_alert=True)

    # Проверка обязательной подписки
    if c["require_sub"]:
        not_subbed = await check_subscriptions(call.bot, call.from_user.id, cid)
        if not_subbed:
            ch_list = "\n".join(f"• {t}" for t in not_subbed)
            return await call.answer(
                f"❌ Для участия подпишитесь на:\n{ch_list}",
                show_alert=True
            )

    check = await db_one("SELECT id FROM participants WHERE contest_id=? AND user_id=?",
                         (cid, call.from_user.id))
    if check:
        await db_run("DELETE FROM participants WHERE contest_id=? AND user_id=?",
                     (cid, call.from_user.id))
        await call.answer("❌ Вы вышли из конкурса.", show_alert=True)
    else:
        await db_run(
            "INSERT OR IGNORE INTO participants (contest_id, user_id, name, username) VALUES (?,?,?,?)",
            (cid, call.from_user.id, call.from_user.full_name, call.from_user.username)
        )
        await call.answer("✅ Вы участвуете!", show_alert=True)

# ── PARTICIPANTS LIST ─────────────────────────────────────

@dp.callback_query(F.data.startswith("plist:"))
async def plist(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    rows = await db_get("SELECT * FROM participants WHERE contest_id=?", (cid,))
    if not rows:
        return await call.answer("👥 Участников нет.", show_alert=True)
    lines = [f"{i+1}. {r['name']} (@{r['username'] or '—'})" for i, r in enumerate(rows[:50])]
    await call.answer()
    await call.message.answer(
        f"👥 <b>Участники ({len(rows)}):</b>\n\n" + "\n".join(lines),
        parse_mode="HTML"
    )

# ── DRAW ─────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("draw:"))
async def draw_ask(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    n = await db_one("SELECT COUNT(*) as n FROM participants WHERE contest_id=?", (cid,))
    await call.message.edit_text(
        f"🏆 Провести розыгрыш?\n\n<b>{c['title']}</b>\nУчастников: {n['n']}",
        parse_mode="HTML",
        reply_markup=confirm_kb(cid, "draw")
    )

def winner_line(w, idx):
    """Строка победителя с кликабельным именем на профиль."""
    medals = ["🥇", "🥈", "🥉"]
    medal  = medals[idx] if idx < 3 else "🏅"
    if w["username"]:
        return f'{medal} <a href="https://t.me/{w["username"]}">{w["name"]}</a>'
    else:
        return f'{medal} <a href="tg://user?id={w["user_id"]}">{w["name"]}</a>'

async def run_draw(bot: Bot, cid: int) -> str | None:
    """Проводит розыгрыш, публикует результат, уведомляет победителей.
    Возвращает текст результата или None если участников нет."""
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return None
    rows = await db_get("SELECT * FROM participants WHERE contest_id=?", (cid,))
    if not rows:
        return None

    count   = min(c["winners_count"], len(rows))
    winners = random.sample(list(rows), count)
    await db_run("UPDATE contests SET status='finished' WHERE id=?", (cid,))

    lines  = [winner_line(w, i) for i, w in enumerate(winners)]
    result = (
        f"Результаты конкурса: 🥳\n\n"
        f"<b>{c['title']}</b>\n\n"
        f"Победитель:\n"
        + "\n".join(lines)
    )

    # Публикуем в канал
    if c["channel_id"]:
        try:
            await bot.send_message(c["channel_id"], result, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Result post error: {e}")

    winner_ids = {w["user_id"] for w in winners}

    # Уведомляем ВСЕХ участников
    notified = 0
    for p in rows:
        is_winner = p["user_id"] in winner_ids
        if is_winner:
            personal_msg = (
                f"🎉 <b>Поздравляем! Вы победитель!</b>\n\n"
                f"🏆 Конкурс: <b>{c['title']}</b>\n\n"
                f"{result}\n\n"
                "Организатор свяжется с вами в ближайшее время."
            )
        else:
            personal_msg = (
                f"🏁 Конкурс <b>{c['title']}</b> завершён!\n\n"
                f"{result}\n\n"
                "Спасибо за участие! 🙏"
            )
        try:
            await bot.send_message(p["user_id"], personal_msg, parse_mode="HTML")
            if is_winner:
                notified += 1
        except Exception as e:
            logging.warning(f"Could not notify participant {p['user_id']}: {e}")

    return result, notified

@dp.callback_query(F.data.startswith("draw_yes:"))
async def draw_run(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)

    rows = await db_get("SELECT * FROM participants WHERE contest_id=?", (cid,))
    if not rows:
        return await call.message.edit_text("⚠️ Нет участников.")

    res = await run_draw(call.bot, cid)
    if not res:
        return await call.message.edit_text("⚠️ Нет участников.")

    result_text, notified = res
    await call.message.edit_text(result_text, parse_mode="HTML")
    if notified:
        await call.message.answer(f"📨 Уведомления отправлены {notified} победителям в личку.")

# ── EDIT ─────────────────────────────────────────────────

@dp.message(Command("editlot"))
async def editlot(msg: Message):
    rows = await db_get("SELECT * FROM contests WHERE owner_id=? ORDER BY id DESC", (msg.from_user.id,))
    if not rows:
        return await msg.answer("Нет конкурсов.")
    await msg.answer("✏️ Выберите конкурс:", reply_markup=list_kb(rows, prefix="edit"))

@dp.callback_query(F.data.startswith("edit:"))
async def edit_menu(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    await call.message.edit_text(
        f"✏️ <b>Редактирование:</b> {c['title']}\n\nЧто изменить?",
        parse_mode="HTML",
        reply_markup=edit_kb(cid)
    )

@dp.callback_query(F.data.startswith("ef:"))
async def edit_field_start(call: CallbackQuery, state: FSMContext):
    parts = call.data.split(":")
    field = parts[1]
    cid   = int(parts[2])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)

    # Редактирование обязательной подписки — отдельный сценарий
    if field == "req_sub":
        await state.set_state(Edit.value)
        await state.update_data(field="req_sub", cid=cid)
        channels = await db_get("SELECT * FROM channels WHERE owner_id=?", (call.from_user.id,))
        if not channels:
            return await call.answer("Нет каналов. Добавьте в разделе 📢 Мои каналы.", show_alert=True)
        req = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (cid,))
        selected_ids = [r["channel_id"] for r in req]
        await call.message.answer("🔒 Выберите каналы для обязательной подписки:",
                                  reply_markup=req_subs_kb(channels, selected_ids))
        await call.answer()
        return

    await state.set_state(Edit.value)
    await state.update_data(field=field, cid=cid)

    prompts = {
        "title":         "📝 Введите новое название:",
        "desc":          "📄 Отправьте новое описание (текст, фото или видео):",
        "winners_count": "👑 Введите новое количество победителей:",
        "btn_text":      "🎟 Введите новый текст кнопки участия:",
        "pub_time":      "⏰ Введите новое время публикации (МСК):\nПримеры: <code>13:09</code> или <code>25.12 13:09</code>",
        "end_time":      "🏁 Введите новое время окончания (МСК):\nПримеры: <code>13:09</code> или <code>25.12 20:00</code>",
        "channel":       "📢 Выберите новый канал:"
    }

    if field == "channel":
        channels = await db_get("SELECT * FROM channels WHERE owner_id=?", (call.from_user.id,))
        if not channels:
            return await call.answer("Нет каналов. Добавьте в разделе 📢 Мои каналы.", show_alert=True)
        await call.message.answer("📢 Выберите канал:", reply_markup=channels_kb(channels, prefix="seledit"))
    else:
        await call.message.answer(prompts.get(field, "Введите значение:"), reply_markup=cancel_kb())
    await call.answer()

# Редактирование обязательной подписки через toggle в Edit состоянии
@dp.callback_query(Edit.value, F.data.startswith("rsc_toggle:"))
async def edit_rsc_toggle(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("field") != "req_sub":
        return
    chan_id = call.data.split(":", 1)[1]
    cid     = data["cid"]
    req     = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (cid,))
    ids     = [r["channel_id"] for r in req]
    if chan_id in ids:
        await db_run("DELETE FROM required_channels WHERE contest_id=? AND channel_id=?", (cid, chan_id))
    else:
        ch = await db_one("SELECT * FROM channels WHERE channel_id=?", (chan_id,))
        await db_run(
            "INSERT OR IGNORE INTO required_channels (contest_id, channel_id, title) VALUES (?,?,?)",
            (cid, chan_id, ch["title"] if ch else chan_id)
        )
        await db_run("UPDATE contests SET require_sub=1 WHERE id=?", (cid,))
    req_new      = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (cid,))
    selected_ids = [r["channel_id"] for r in req_new]
    channels     = await db_get("SELECT * FROM channels WHERE owner_id=?", (call.from_user.id,))
    await call.message.edit_reply_markup(reply_markup=req_subs_kb(channels, selected_ids))
    await call.answer()

@dp.callback_query(Edit.value, F.data.in_({"rsc_done", "rsc_skip"}))
async def edit_rsc_done(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("field") != "req_sub":
        return
    cid = data["cid"]
    req = await db_get("SELECT * FROM required_channels WHERE contest_id=?", (cid,))
    await db_run("UPDATE contests SET require_sub=? WHERE id=?", (1 if req else 0, cid))
    await state.clear()
    await call.message.answer("✅ Подписка обновлена!", reply_markup=main_kb(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data.startswith("selchan:"))
async def select_chan_for_contest(call: CallbackQuery, state: FSMContext):
    chan_id = call.data.split(":", 1)[1]
    ch = await db_one("SELECT * FROM channels WHERE channel_id=?", (chan_id,))
    data = await state.get_data()
    if data.get("cid"):
        cid = data["cid"]
        await db_run("UPDATE contests SET channel_id=?, channel_title=? WHERE id=?",
                     (chan_id, ch["title"] if ch else chan_id, cid))
        await state.clear()
        await call.message.answer("✅ Канал обновлён!", reply_markup=main_kb(call.from_user.id))
    await call.answer()

@dp.callback_query(F.data.startswith("seledit:"))
async def select_chan_edit(call: CallbackQuery, state: FSMContext):
    chan_id = call.data.split(":", 1)[1]
    ch = await db_one("SELECT * FROM channels WHERE channel_id=?", (chan_id,))
    data = await state.get_data()
    cid = data.get("cid")
    if cid:
        await db_run("UPDATE contests SET channel_id=?, channel_title=? WHERE id=?",
                     (chan_id, ch["title"] if ch else chan_id, cid))
        await state.clear()
        await call.message.answer("✅ Канал обновлён!", reply_markup=main_kb(call.from_user.id))
    await call.answer()

@dp.message(Edit.value)
async def edit_save(msg: Message, state: FSMContext):
    if msg.text == "❌ Отмена":
        await state.clear()
        return await msg.answer("Отменено.", reply_markup=main_kb(msg.from_user.id))

    data  = await state.get_data()
    field = data["field"]
    cid   = data["cid"]

    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if c["owner_id"] != msg.from_user.id and msg.from_user.id not in ADMINS:
        await state.clear()
        return await msg.answer("⛔ Нет доступа.", reply_markup=main_kb(msg.from_user.id))

    if field == "desc":
        if msg.photo:
            await db_run("UPDATE contests SET photo_id=?, video_id=NULL, description=? WHERE id=?",
                         (msg.photo[-1].file_id, msg.caption, cid))
        elif msg.video:
            await db_run("UPDATE contests SET video_id=?, photo_id=NULL, description=? WHERE id=?",
                         (msg.video.file_id, msg.caption, cid))
        else:
            await db_run("UPDATE contests SET description=?, photo_id=NULL, video_id=NULL WHERE id=?",
                         (msg.text, cid))
    elif field == "winners_count":
        if not msg.text.isdigit() or int(msg.text) < 1:
            return await msg.answer("⚠️ Введите число от 1.")
        await db_run("UPDATE contests SET winners_count=? WHERE id=?", (int(msg.text), cid))
    elif field in ("pub_time", "end_time"):
        dt = parse_time(msg.text)
        if not dt:
            return await msg.answer("⚠️ Неверный формат. Пример: <code>25.12 18:00</code>",
                                    parse_mode="HTML")
        await db_run(f"UPDATE contests SET {field}=? WHERE id=?", (dt.isoformat(), cid))
    else:
        await db_run(f"UPDATE contests SET {field}=? WHERE id=?", (msg.text, cid))

    await state.clear()
    await msg.answer("✅ Сохранено!", reply_markup=main_kb(msg.from_user.id))

# ── DELETE ────────────────────────────────────────────────

@dp.message(Command("dellot"))
async def dellot(msg: Message):
    rows = await db_get("SELECT * FROM contests WHERE owner_id=? ORDER BY id DESC", (msg.from_user.id,))
    if not rows:
        return await msg.answer("Нет конкурсов.")
    await msg.answer("🗑 Выберите конкурс:", reply_markup=list_kb(rows, prefix="del"))

@dp.callback_query(F.data.startswith("del:"))
async def del_ask(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if not c:
        return await call.answer("Не найден.", show_alert=True)
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    await call.message.edit_text(
        f"🗑 Удалить <b>{c['title']}</b>?",
        parse_mode="HTML",
        reply_markup=confirm_kb(cid, "del")
    )

@dp.callback_query(F.data.startswith("del_yes:"))
async def del_run(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    c = await db_one("SELECT * FROM contests WHERE id=?", (cid,))
    if c["owner_id"] != call.from_user.id and call.from_user.id not in ADMINS:
        return await call.answer("⛔ Нет доступа.", show_alert=True)
    await db_run("DELETE FROM participants WHERE contest_id=?", (cid,))
    await db_run("DELETE FROM required_channels WHERE contest_id=?", (cid,))
    await db_run("DELETE FROM contests WHERE id=?", (cid,))
    await call.message.edit_text("✅ Конкурс удалён.")

# ── ADMIN PANEL ───────────────────────────────────────────

@dp.message(F.text == "⚙️ Админ панель")
@dp.message(Command("admin"))
async def admin_panel(msg: Message):
    if msg.from_user.id not in ADMINS:
        return await msg.answer("⛔ Нет доступа.")
    total  = await db_one("SELECT COUNT(*) as n FROM contests")
    active = await db_one("SELECT COUNT(*) as n FROM contests WHERE status='active'")
    parts  = await db_one("SELECT COUNT(*) as n FROM participants")
    await msg.answer(
        "⚙️ <b>Админ панель</b>\n\n"
        f"📋 Всего конкурсов: <b>{total['n']}</b>\n"
        f"✅ Активных: <b>{active['n']}</b>\n"
        f"👥 Участий: <b>{parts['n']}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все конкурсы", callback_data="admin_all")]
        ])
    )

@dp.callback_query(F.data == "admin_all")
async def admin_all(call: CallbackQuery):
    if call.from_user.id not in ADMINS:
        return await call.answer("⛔", show_alert=True)
    rows = await db_get("SELECT * FROM contests ORDER BY id DESC")
    if not rows:
        return await call.answer("Конкурсов нет.", show_alert=True)
    await call.message.edit_text(
        f"📋 <b>Все конкурсы ({len(rows)}):</b>",
        parse_mode="HTML",
        reply_markup=list_kb(rows)
    )

# ── SCHEDULER ─────────────────────────────────────────────

async def scheduler(bot: Bot):
    while True:
        await asyncio.sleep(30)
        try:
            now = now_msk()
            drafts = await db_get(
                "SELECT * FROM contests WHERE status='draft' AND pub_time IS NOT NULL"
            )
            for c in drafts:
                pub_dt = datetime.fromisoformat(c["pub_time"])
                if now >= pub_dt:
                    mid = await publish_contest(bot, c)
                    if mid:
                        await db_run(
                            "UPDATE contests SET status='active', message_id=? WHERE id=?",
                            (mid, c["id"])
                        )
                        logging.info(f"Published contest {c['id']}")

            active = await db_get(
                "SELECT * FROM contests WHERE status='active' AND end_time IS NOT NULL"
            )
            for c in active:
                end_dt = datetime.fromisoformat(c["end_time"])
                if now >= end_dt:
                    logging.info(f"Auto-draw contest {c['id']}")
                    rows = await db_get("SELECT * FROM participants WHERE contest_id=?", (c["id"],))
                    if rows:
                        res = await run_draw(bot, c["id"])
                        if res:
                            result_text, notified = res
                            # Уведомляем организатора
                            try:
                                await bot.send_message(
                                    c["owner_id"],
                                    f"🏁 Конкурс <b>{c['title']}</b> завершён!\n\n"
                                    f"{result_text}\n\n"
                                    f"📨 Победителей уведомлено: {notified}",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                logging.error(f"Owner notify error: {e}")
                    else:
                        await db_run("UPDATE contests SET status='finished' WHERE id=?", (c["id"],))
                        if c["channel_id"]:
                            try:
                                await bot.send_message(
                                    c["channel_id"],
                                    f"🏁 Конкурс <b>{c['title']}</b> завершён!\n"
                                    "К сожалению, участников не было.",
                                    parse_mode="HTML"
                                )
                            except Exception as e:
                                logging.error(f"End notify error: {e}")
                        try:
                            await bot.send_message(
                                c["owner_id"],
                                f"🏁 Конкурс <b>{c['title']}</b> завершён.\n"
                                "Участников не было — розыгрыш не проводился.",
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logging.error(f"Owner notify error: {e}")
                    logging.info(f"Finished contest {c['id']}")
        except Exception as e:
            logging.error(f"Scheduler error: {e}")

# ── MAIN ─────────────────────────────────────────────────

async def main():
    await db_init()
    asyncio.ensure_future(scheduler(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
