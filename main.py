import os
import re
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

import asyncpg
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi. Railway Variables ga BOT_TOKEN qo‘ying.")

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL topilmadi. Railway Postgres ulab, vote-bot service Variables ga DATABASE_URL qo‘ying.")

# Adminlar ro‘yxati (user_id)
ADMINS = [32257986]

UTC = timezone.utc

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

pool: asyncpg.Pool = None  # type: ignore

# ===================== DB INIT =====================
DDL = """
CREATE TABLE IF NOT EXISTS channels(
    chat_id TEXT PRIMARY KEY,     -- '@kanal' yoki '-100...'
    join_url TEXT                 -- ixtiyoriy (private kanal uchun majburiy)
);

CREATE TABLE IF NOT EXISTS candidates(
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS votes(
    user_id BIGINT PRIMARY KEY,
    candidate_id INT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    ts TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

# ===================== FSM =====================
class AdminState(StatesGroup):
    add_channel = State()
    remove_channel = State()
    add_candidate = State()
    remove_candidate = State()
    set_timer = State()

# ===================== Helpers =====================
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def now_utc() -> datetime:
    return datetime.now(UTC)

async def db_exec(query: str, *args):
    async with pool.acquire() as con:
        await con.execute(query, *args)

async def db_fetch(query: str, *args):
    async with pool.acquire() as con:
        return await con.fetch(query, *args)

async def db_fetchrow(query: str, *args):
    async with pool.acquire() as con:
        return await con.fetchrow(query, *args)

async def get_end_time() -> Optional[datetime]:
    row = await db_fetchrow("SELECT value FROM settings WHERE key='end_time_utc'")
    if not row or not row["value"]:
        return None
    try:
        # ISO format saqlangan bo‘ladi
        return datetime.fromisoformat(row["value"])
    except Exception:
        return None

async def voting_is_open() -> bool:
    end_time = await get_end_time()
    if not end_time:
        return True
    return now_utc() < end_time

async def remaining_time_text() -> str:
    end_time = await get_end_time()
    if not end_time:
        return "⏳ Taymer: o‘rnatilmagan (ovoz berish ochiq)"
    delta = end_time - now_utc()
    if delta.total_seconds() <= 0:
        return "⏳ Taymer: tugagan (ovoz berish yopiq)"
    mins = int(delta.total_seconds() // 60)
    secs = int(delta.total_seconds() % 60)
    return f"⏳ Qolgan vaqt: <b>{mins:02d}:{secs:02d}</b>"

def parse_channel_input(text: str) -> Tuple[str, Optional[str]]:
    """
    Qabul qiladi:
      @username
      -100123...
      https://t.me/username
      t.me/username
      -100123... https://t.me/+invite
      @username https://t.me/username
    Natija: (chat_id, join_url)
    """
    parts = text.strip().split()
    first = parts[0].strip()
    join_url = parts[1].strip() if len(parts) > 1 else None

    # URL bo‘lsa username ni ajratamiz
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/?$", first)
    if m:
        username = m.group(1)
        chat_id = f"@{username}"
        if not join_url:
            join_url = f"https://t.me/{username}"
        return chat_id, join_url

    # @username bo‘lsa
    if first.startswith("@"):
        chat_id = first
        if not join_url:
            join_url = f"https://t.me/{first.lstrip('@')}"
        return chat_id, join_url

    # -100... bo‘lsa (private kanal)
    if re.fullmatch(r"-100\d{5,}", first):
        chat_id = first
        return chat_id, join_url  # private uchun join_url tavsiya/kerak bo‘lishi mumkin

    # Aks holda xato
    raise ValueError("Kanal formati noto‘g‘ri")

async def get_channels() -> List[Tuple[str, Optional[str]]]:
    rows = await db_fetch("SELECT chat_id, join_url FROM channels ORDER BY chat_id ASC")
    return [(r["chat_id"], r["join_url"]) for r in rows]

async def is_subscribed(user_id: int) -> bool:
    channels = await get_channels()
    if not channels:
        return True

    for chat_id, _url in channels:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            # Bot kanalga admin bo‘lmasa yoki chat_id noto‘g‘ri bo‘lsa xato bo‘lishi mumkin
            return False
    return True

def subscribe_kb(channels: List[Tuple[str, Optional[str]]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    has_any = False
    for chat_id, join_url in channels:
        url = join_url
        if not url and isinstance(chat_id, str) and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            has_any = True
            kb.add(InlineKeyboardButton(text=f"➕ Obuna bo‘lish: {chat_id}", url=url))

    if not has_any:
        kb.add(InlineKeyboardButton("⚠️ Kanal linki yo‘q (admin link qo‘shsin)", callback_data="noop"))

    kb.add(InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub"))
    return kb

async def candidates_with_counts() -> List[Tuple[int, str, int]]:
    rows = await db_fetch("""
        SELECT c.id AS id, c.name AS name, COUNT(v.candidate_id) AS cnt
        FROM candidates c
        LEFT JOIN votes v ON v.candidate_id = c.id
        GROUP BY c.id
        ORDER BY c.id ASC
    """)
    return [(int(r["id"]), str(r["name"]), int(r["cnt"])) for r in rows]

async def total_votes() -> int:
    row = await db_fetchrow("SELECT COUNT(*) AS c FROM votes")
    return int(row["c"]) if row else 0

async def voting_message_text() -> str:
    open_state = "✅ Ovoz berish: <b>ochiq</b>" if (await voting_is_open()) else "⛔ Ovoz berish: <b>yopiq</b>"
    return (
        "🗳 <b>Ovoz berish</b>\n\n"
        "Nomzodni tanlang:\n\n"
        f"🧮 Jami ovozlar: <b>{await total_votes()}</b>\n"
        f"{await remaining_time_text()}\n"
        f"{open_state}"
    )

async def live_vote_kb(disabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    rows = await candidates_with_counts()

    if not rows:
        kb.add(InlineKeyboardButton("⛔ Nomzodlar yo‘q (admin qo‘shadi)", callback_data="noop"))
        return kb

    # 2 ustun qilib chiqamiz: [btn, btn] satrlar
    buttons = []
    for idx, (cid, name, cnt) in enumerate(rows, start=1):
        cb = "noop" if disabled else f"vote:{cid}"
        text = f"{idx}. {name} • {cnt}"
        buttons.append(InlineKeyboardButton(text=text, callback_data=cb))

    # row_width=2 bo‘lsa ham, o‘zimiz aniq qo‘shamiz
    for i in range(0, len(buttons), 2):
        if i + 1 < len(buttons):
            kb.row(buttons[i], buttons[i + 1])
        else:
            kb.row(buttons[i])

    return kb

def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Kanal", callback_data="a:add_channel"),
        InlineKeyboardButton("➖ Kanal", callback_data="a:rm_channel"),
    )
    kb.add(
        InlineKeyboardButton("📃 Kanallar", callback_data="a:list_channels"),
        InlineKeyboardButton("📃 Nomzodlar", callback_data="a:list_candidates"),
    )
    kb.add(
        InlineKeyboardButton("➕ Nomzod", callback_data="a:add_candidate"),
        InlineKeyboardButton("➖ Nomzod", callback_data="a:rm_candidate"),
    )
    kb.add(
        InlineKeyboardButton("⏳ Taymer (daq)", callback_data="a:set_timer"),
        InlineKeyboardButton("🛑 Taymer stop", callback_data="a:stop_timer"),
    )
    kb.add(
        InlineKeyboardButton("📊 Natijalar", callback_data="a:results"),
        InlineKeyboardButton("🗑 Ovozlarni 0 qilish", callback_data="a:reset_votes"),
    )
    return kb

def deep_link_for_candidate(me_username: Optional[str], cid: int) -> Optional[str]:
    if not me_username:
        return None
    # /start c<id> orqali o‘tadi
    return f"https://t.me/{me_username}?start=c{cid}"

def bar(pct: float, width: int = 12) -> str:
    # oddiy progress bar
    filled = int(round(pct / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)

async def results_text_and_kb() -> Tuple[str, InlineKeyboardMarkup]:
    rows = await candidates_with_counts()
    total = sum(cnt for _cid, _name, cnt in rows)

    if not rows:
        return ("Natija yo‘q (nomzodlar yo‘q).", InlineKeyboardMarkup())

    # Bot username (deep link uchun)
    me = await bot.get_me()
    me_username = me.username

    text = "<b>📊 Natijalar (real time)</b>\n"
    text += f"🧮 Jami ovoz: <b>{total}</b>\n"
    text += f"{await remaining_time_text()}\n\n"

    # Natijani cnt bo‘yicha saralaymiz
    sorted_rows = sorted(rows, key=lambda x: (-x[2], x[0]))

    for rank, (cid, name, cnt) in enumerate(sorted_rows, start=1):
        pct = (cnt / total * 100) if total else 0.0
        text += f"{rank}. <b>{name}</b> — <b>{cnt}</b> ({pct:.1f}%)\n"
        text += f"   {bar(pct)}\n"

    text += "\n<i>Nomzodni bosib botga o‘tib ovoz berishingiz mumkin.</i>"

    kb = InlineKeyboardMarkup(row_width=2)
    # klaviaturaga nomzodlar: bosilganda botga deep link
    btns = []
    for idx, (cid, name, cnt) in enumerate(rows, start=1):
        url = deep_link_for_candidate(me_username, cid)
        label = f"{idx}. {cnt}"
        if url:
            btns.append(InlineKeyboardButton(text=label, url=url))
        else:
            # fallback
            btns.append(InlineKeyboardButton(text=label, callback_data=f"open_c:{cid}"))

    for i in range(0, len(btns), 2):
        if i + 1 < len(btns):
            kb.row(btns[i], btns[i + 1])
        else:
            kb.row(btns[i])

    return text, kb

# ===================== USER FLOW =====================
@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message):
    args = (m.get_args() or "").strip()
    # deep link: c<id>
    if args.startswith("c") and args[1:].isdigit():
        cid = int(args[1:])
        # nomzodni tekshiramiz
        row = await db_fetchrow("SELECT id, name FROM candidates WHERE id=$1", cid)
        if not row:
            await m.answer("❗ Nomzod topilmadi.")
            return

        # Avval obuna bo‘lish shart
        channels = await get_channels()
        if not await is_subscribed(m.from_user.id):
            await m.answer(
                "❗ Ovoz berish uchun avval quyidagi kanal(lar)ga obuna bo‘ling va <b>✅ Tekshirish</b>ni bosing:",
                reply_markup=subscribe_kb(channels),
            )
            return

        if not await voting_is_open():
            await m.answer("⛔ Ovoz berish yakunlangan.")
            return

        await m.answer(
            f"🗳 Nomzod: <b>{row['name']}</b>\n\nOvoz berishni tasdiqlang:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("✅ Ovoz berish", callback_data=f"vote:{cid}")
            ),
        )
        return

    await m.answer(
        "Assalomu alaykum!\n\nOvoz berish uchun quyidagi tugmani bosing:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🗳 Ovoz berish", callback_data="open_vote")
        ),
    )

@dp.callback_query_handler(lambda c: c.data == "open_vote")
async def cb_open_vote(c: types.CallbackQuery):
    await c.answer()
    channels = await get_channels()
    if not await is_subscribed(c.from_user.id):
        await c.message.answer(
            "❗ Ovoz berish uchun avval quyidagi kanal(lar)ga obuna bo‘ling va <b>✅ Tekshirish</b>ni bosing:",
            reply_markup=subscribe_kb(channels),
        )
        return
    await c.message.answer(await voting_message_text(), reply_markup=await live_vote_kb(disabled=not (await voting_is_open())))

@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check_sub(c: types.CallbackQuery):
    ok = await is_subscribed(c.from_user.id)
    if ok:
        await c.answer("✅ Obuna tasdiqlandi!", show_alert=True)
        await c.message.answer(await voting_message_text(), reply_markup=await live_vote_kb(disabled=not (await voting_is_open())))
    else:
        await c.answer("❗ Avval barcha kanallarga obuna bo‘ling.\n(Bot kanallarda admin bo‘lishi kerak)", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith("open_c:"))
async def cb_open_candidate_fallback(c: types.CallbackQuery):
    # deep link bo‘lmaganda fallback: shu chatda nomzod oynasi
    await c.answer()
    cid = int(c.data.split(":")[1])
    row = await db_fetchrow("SELECT id, name FROM candidates WHERE id=$1", cid)
    if not row:
        await c.message.answer("❗ Nomzod topilmadi.")
        return

    channels = await get_channels()
    if not await is_subscribed(c.from_user.id):
        await c.message.answer(
            "❗ Ovoz berish uchun avval quyidagi kanal(lar)ga obuna bo‘ling va <b>✅ Tekshirish</b>ni bosing:",
            reply_markup=subscribe_kb(channels),
        )
        return

    if not await voting_is_open():
        await c.message.answer("⛔ Ovoz berish yakunlangan.")
        return

    await c.message.answer(
        f"🗳 Nomzod: <b>{row['name']}</b>\n\nOvoz berishni tasdiqlang:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("✅ Ovoz berish", callback_data=f"vote:{cid}")
        ),
    )

@dp.callback_query_handler(lambda c: c.data.startswith("vote:"))
async def cb_vote(c: types.CallbackQuery):
    if not await voting_is_open():
        await c.answer("⛔ Ovoz berish yakunlangan.", show_alert=True)
        return

    channels = await get_channels()
    if not await is_subscribed(c.from_user.id):
        await c.answer("❗ Ovoz berishdan oldin kanallarga obuna bo‘ling.", show_alert=True)
        await c.message.answer(
            "❗ Ovoz berish uchun avval quyidagi kanal(lar)ga obuna bo‘ling va <b>✅ Tekshirish</b>ni bosing:",
            reply_markup=subscribe_kb(channels),
        )
        return

    try:
        cid = int(c.data.split(":")[1])
    except ValueError:
        await c.answer("Xatolik", show_alert=True)
        return

    row = await db_fetchrow("SELECT id, name FROM candidates WHERE id=$1", cid)
    if not row:
        await c.answer("Nomzod topilmadi", show_alert=True)
        return

    # 1 odam = 1 ovoz (UPDATE qilib nomzod almashishini ham xohlasangiz ayting)
    try:
        await db_exec(
            "INSERT INTO votes(user_id, candidate_id, ts) VALUES($1,$2,NOW())",
            int(c.from_user.id), int(cid)
        )
        await c.answer("✅ Ovoz qabul qilindi!", show_alert=True)
    except asyncpg.UniqueViolationError:
        await c.answer("❗ Siz allaqachon ovoz bergansiz.", show_alert=True)
    except Exception:
        await c.answer("❌ Xatolik. Keyinroq urinib ko‘ring.", show_alert=True)

    # real-time yangilash
    try:
        await c.message.edit_text(await voting_message_text(), reply_markup=await live_vote_kb(disabled=False))
    except Exception:
        pass

@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(c: types.CallbackQuery):
    await c.answer()

# ===================== ADMIN FLOW =====================
@dp.message_handler(commands=["admin"])
async def cmd_admin(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⚙️ <b>Admin panel</b>", reply_markup=admin_kb())

@dp.callback_query_handler(lambda c: c.data.startswith("a:"))
async def cb_admin_actions(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Kirish yo‘q", show_alert=True)
        return

    action = c.data.split(":", 1)[1]
    await c.answer()

    if action == "add_channel":
        await AdminState.add_channel.set()
        await c.message.answer(
            "Kanal qo‘shish.\n\n"
            "Yuboring:\n"
            "• <b>@publickanal</b>\n"
            "yoki\n"
            "• <b>-1001234567890</b> (private)\n"
            "yoki\n"
            "• <b>https://t.me/publickanal</b>\n\n"
            "Ixtiyoriy: private kanal uchun invite link qo‘shing:\n"
            "<code>-100123... https://t.me/+invite</code>"
        )

    elif action == "rm_channel":
        await AdminState.remove_channel.set()
        await c.message.answer("O‘chirish uchun kanalni yuboring: <b>@username</b> yoki <b>-100...</b> yoki <b>https://t.me/username</b>")

    elif action == "list_channels":
        rows = await get_channels()
        if not rows:
            await c.message.answer("Kanallar ro‘yxati bo‘sh.")
        else:
            text = "<b>📃 Kanallar:</b>\n"
            for chat_id, url in rows:
                text += f"• <code>{chat_id}</code>"
                if url:
                    text += f"\n  {url}"
                text += "\n"
            await c.message.answer(text)

    elif action == "add_candidate":
        await AdminState.add_candidate.set()
        await c.message.answer(
            "Nomzod qo‘shish.\n\n"
            "✅ Birdaniga qo‘shish mumkin:\n"
            "Har qatorga bitta nomzod yozing.\n\n"
            "Misol:\n"
            "<code>Davronbek MFY\nShaldiramoq MFY\nTolzor MFY</code>"
        )

    elif action == "rm_candidate":
        await AdminState.remove_candidate.set()
        await c.message.answer("O‘chirish uchun nomzod ID’sini yuboring (ID ni «📃 Nomzodlar»dan ko‘rasiz).")

    elif action == "list_candidates":
        rows = await db_fetch("SELECT id, name FROM candidates ORDER BY id ASC")
        if not rows:
            await c.message.answer("Nomzodlar yo‘q.")
        else:
            text = "<b>📃 Nomzodlar:</b>\n"
            for idx, r in enumerate(rows, start=1):
                text += f'{idx}. "{r["name"]}" (ID: {r["id"]})\n'
            await c.message.answer(text)

    elif action == "set_timer":
        await AdminState.set_timer.set()
        await c.message.answer(
            "Taymer o‘rnatish (daqiqada).\n"
            "Masalan: <code>60</code> (60 daqiqa)\n\n"
            "⚠️ Taymer tugasa ovoz berish yopiladi."
        )

    elif action == "stop_timer":
        await db_exec("DELETE FROM settings WHERE key='end_time_utc'")
        await c.message.answer("🛑 Taymer o‘chirildi. Ovoz berish ochiq (cheklanmagan).")

    elif action == "reset_votes":
        await db_exec("DELETE FROM votes")
        await c.message.answer("🗑 Barcha ovozlar 0 qilindi.")

    elif action == "results":
        text, kb = await results_text_and_kb()
        await c.message.answer(text, reply_markup=kb)

@dp.message_handler(state=AdminState.add_channel)
async def st_add_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        chat_id, join_url = parse_channel_input(m.text)
        await db_exec("INSERT INTO channels(chat_id, join_url) VALUES($1,$2) ON CONFLICT (chat_id) DO UPDATE SET join_url=EXCLUDED.join_url",
                      chat_id, join_url)
        await m.answer(f"✅ Kanal qo‘shildi: <code>{chat_id}</code>" + (f"\n{join_url}" if join_url else ""))
        await m.answer("⚠️ Obuna tekshirish ishlashi uchun botni shu kanalga ADMIN qilib qo‘ying.")
    except Exception as e:
        await m.answer(f"❌ Xatolik. Formatni tekshiring.\n<i>{e}</i>")
    await state.finish()

@dp.message_handler(state=AdminState.remove_channel)
async def st_rm_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    text = m.text.strip()
    try:
        chat_id, _join = parse_channel_input(text)
    except Exception:
        chat_id = text
    await db_exec("DELETE FROM channels WHERE chat_id=$1", chat_id)
    await m.answer(f"✅ Kanal o‘chirildi (bo‘lgan bo‘lsa): <code>{chat_id}</code>")
    await state.finish()

@dp.message_handler(state=AdminState.add_candidate)
async def st_add_candidate(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return

    lines = [ln.strip() for ln in (m.text or "").splitlines()]
    names = [ln for ln in lines if ln]

    if not names:
        await m.answer("❌ Hech narsa topilmadi. Har qatorda bitta nomzod bo‘lsin.")
        return

    async with pool.acquire() as con:
        async with con.transaction():
            for name in names:
                await con.execute("INSERT INTO candidates(name) VALUES($1)", name)

    await m.answer(f"✅ Nomzod(lar) qo‘shildi: <b>{len(names)}</b> ta")
    # ro‘yxatdan qisqa preview
    preview = "\n".join([f"• {n}" for n in names[:10]])
    if len(names) > 10:
        preview += "\n..."
    await m.answer(f"<b>Preview:</b>\n{preview}")
    await state.finish()

@dp.message_handler(state=AdminState.remove_candidate)
async def st_rm_candidate(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        cid = int(m.text.strip())
    except ValueError:
        await m.answer("❌ ID raqam bo‘lishi kerak. Masalan: <code>3</code>")
        return
    await db_exec("DELETE FROM candidates WHERE id=$1", cid)
    await m.answer(f"✅ Nomzod o‘chirildi (bo‘lgan bo‘lsa): <code>{cid}</code>")
    await state.finish()

@dp.message_handler(state=AdminState.set_timer)
async def st_set_timer(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        mins = int(m.text.strip())
        if mins <= 0:
            raise ValueError()
    except ValueError:
        await m.answer("❌ Daqiqa musbat son bo‘lsin. Masalan: <code>30</code>")
        return

    end_time = now_utc() + timedelta(minutes=mins)
    await db_exec(
        "INSERT INTO settings(key, value) VALUES('end_time_utc', $1) "
        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
        end_time.isoformat()
    )
    await m.answer(f"⏳ Taymer o‘rnatildi: <b>{mins}</b> daqiqa.\n{await remaining_time_text()}")
    await state.finish()

# ===================== STARTUP =====================
async def on_startup(_dp: Dispatcher):
    global pool
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
        command_timeout=30,
    )
    async with pool.acquire() as con:
        await con.execute(DDL)

async def on_shutdown(_dp: Dispatcher):
    if pool:
        await pool.close()

# ===================== RUN =====================
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
