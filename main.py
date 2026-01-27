import os
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ----------------- CONFIG -----------------
API_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi. Railway: Service → Variables ga BOT_TOKEN qo‘ying.")

ADMINS = [32257986]  # <-- admin user_id

UTC = timezone.utc

bot = Bot(API_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ----------------- DB -----------------
db = sqlite3.connect("db.sqlite", check_same_thread=False)
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS channels(
    chat_id TEXT PRIMARY KEY,     -- '@kanal' yoki '-100....'
    join_url TEXT                 -- private kanal uchun invite link bo‘lsa shu yerga
)
""")

sql.execute("""
CREATE TABLE IF NOT EXISTS candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
)
""")

sql.execute("""
CREATE TABLE IF NOT EXISTS votes(
    user_id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    ts TEXT NOT NULL
)
""")

sql.execute("""
CREATE TABLE IF NOT EXISTS settings(
    key TEXT PRIMARY KEY,
    value TEXT
)
""")

db.commit()

# ----------------- FSM -----------------
class AdminState(StatesGroup):
    add_channel = State()
    remove_channel = State()
    add_candidate = State()
    remove_candidate = State()
    set_timer = State()

# ----------------- Helpers -----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def now_utc() -> datetime:
    return datetime.now(UTC)

def get_end_time() -> datetime | None:
    sql.execute("SELECT value FROM settings WHERE key='end_time_utc'")
    row = sql.fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None

def voting_is_open() -> bool:
    end_time = get_end_time()
    if not end_time:
        return True
    return now_utc() < end_time

def remaining_time_text() -> str:
    end_time = get_end_time()
    if not end_time:
        return "⏳ Taymer: o‘rnatilmagan (ovoz berish davom etmoqda)"

    total = int((end_time - now_utc()).total_seconds())
    if total <= 0:
        return "⏳ Taymer: tugagan (ovoz berish tugatilgan)"

    days = total // 86400
    rem = total % 86400
    hours = rem // 3600
    rem %= 3600
    minutes = rem // 60
    seconds = rem % 60

    if days > 0:
        return f"⏳ Qolgan vaqt: <b>{days} kun {hours:02d}:{minutes:02d}:{seconds:02d}</b>"
    return f"⏳ Qolgan vaqt: <b>{hours:02d}:{minutes:02d}:{seconds:02d}</b>"

async def is_subscribed(user_id: int) -> bool:
    sql.execute("SELECT chat_id FROM channels")
    channels = [r[0] for r in sql.fetchall()]
    if not channels:
        return True

    for chat_id in channels:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True

def subscribe_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    sql.execute("SELECT chat_id, join_url FROM channels ORDER BY rowid DESC")
    rows = sql.fetchall()

    for chat_id, join_url in rows:
        url = None
        if join_url:
            url = join_url
        elif isinstance(chat_id, str) and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            kb.add(InlineKeyboardButton(text=f"➕ Obuna bo‘lish: {chat_id}", url=url))

    kb.add(InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub"))
    return kb

def live_vote_kb(disabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    sql.execute("""
        SELECT c.id, c.name, COUNT(v.candidate_id) AS cnt
        FROM candidates c
        LEFT JOIN votes v ON v.candidate_id = c.id
        GROUP BY c.id
        ORDER BY c.id ASC
    """)
    rows = sql.fetchall()

    if not rows:
        kb.add(InlineKeyboardButton("⛔ Nomzodlar yo‘q (admin qo‘shadi)", callback_data="noop"))
        return kb

    for cid, name, cnt in rows:
        cb = "noop" if disabled else f"vote:{cid}"
        kb.add(InlineKeyboardButton(text=f"{name} — {cnt}", callback_data=cb))

    return kb

def voting_message_text() -> str:
    open_state = "✅ Ovoz berish: <b>ochiq</b>" if voting_is_open() else "⛔ Ovoz berish: <b>yopiq</b>"
    return (
        "🗳 <b>Ovoz berish</b>\n\n"
        "Nomzodni tanlang:\n\n"
        f"{remaining_time_text()}\n"
        f"{open_state}"
    )

async def refresh_vote_message(msg: types.Message, disabled: bool | None = None):
    """Update timer + results only on clicks."""
    if disabled is None:
        disabled = (not voting_is_open())
    try:
        await msg.edit_text(
            voting_message_text(),
            reply_markup=live_vote_kb(disabled=disabled)
        )
    except Exception:
        pass

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

# ----------------- User flow -----------------
@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message):
    await m.answer(
        "Assalomu alaykum!\n\nOvoz berish uchun tugmani bosing:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🗳 Ovoz berish", callback_data="open_vote")
        )
    )

@dp.callback_query_handler(lambda c: c.data == "open_vote")
async def cb_open_vote(c: types.CallbackQuery):
    await c.answer()
    try:
        await c.message.edit_text(voting_message_text(), reply_markup=live_vote_kb(disabled=not voting_is_open()))
    except Exception:
        await c.message.answer(voting_message_text(), reply_markup=live_vote_kb(disabled=not voting_is_open()))

@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check_sub(c: types.CallbackQuery):
    ok = await is_subscribed(c.from_user.id)
    if ok:
        await c.answer("✅ Obuna tasdiqlandi!", show_alert=True)
        await refresh_vote_message(c.message)
    else:
        await c.answer("❗ Avval barcha kanallarga obuna bo‘ling.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith("vote:"))
async def cb_vote(c: types.CallbackQuery):
    if not voting_is_open():
        await c.answer("⛔ Ovoz berish yakunlangan.", show_alert=True)
        await refresh_vote_message(c.message, disabled=True)
        return

    if not await is_subscribed(c.from_user.id):
        await c.answer("❗ Ovoz berishdan oldin kanallarga obuna bo‘ling.", show_alert=True)
        await c.message.answer(
            "❗ Ovoz berish uchun avval quyidagi kanal(lar)ga obuna bo‘ling va <b>✅ Tekshirish</b>ni bosing:",
            reply_markup=subscribe_kb()
        )
        return

    try:
        cid = int(c.data.split(":")[1])
    except ValueError:
        await c.answer("Xatolik", show_alert=True)
        return

    sql.execute("SELECT name FROM candidates WHERE id=?", (cid,))
    row = sql.fetchone()
    if not row:
        await c.answer("Nomzod topilmadi", show_alert=True)
        return

    try:
        sql.execute(
            "INSERT INTO votes(user_id, candidate_id, ts) VALUES(?,?,?)",
            (c.from_user.id, cid, now_utc().isoformat())
        )
        db.commit()
        await c.answer("✅ Ovoz qabul qilindi!", show_alert=True)
    except sqlite3.IntegrityError:
        await c.answer("❗ Siz allaqachon ovoz bergansiz.", show_alert=True)

    await refresh_vote_message(c.message)

@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(c: types.CallbackQuery):
    await c.answer()

# ----------------- Admin flow -----------------
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
            "• <b>-1001234567890</b> (private)\n\n"
            "Ixtiyoriy: link ham qo‘shing:\n"
            "<code>-100123... https://t.me/+invite</code>"
        )

    elif action == "rm_channel":
        await AdminState.remove_channel.set()
        await c.message.answer("O‘chirish uchun kanalni yuboring: <b>@username</b> yoki <b>-100...</b>")

    elif action == "list_channels":
        sql.execute("SELECT chat_id, COALESCE(join_url,'') FROM channels ORDER BY rowid DESC")
        rows = sql.fetchall()
        if not rows:
            await c.message.answer("Kanallar ro‘yxati bo‘sh.")
        else:
            text = "<b>📃 Kanallar:</b>\n"
            for chat_id, url in rows:
                text += f"• <code>{chat_id}</code>"
                if url:
                    text += " (link bor)"
                text += "\n"
            await c.message.answer(text)

    elif action == "add_candidate":
        await AdminState.add_candidate.set()
        await c.message.answer("Nomzod nomini yuboring.")

    elif action == "rm_candidate":
        await AdminState.remove_candidate.set()
        await c.message.answer("Nomzod ID’sini yuboring (ID ni «📃 Nomzodlar»dan ko‘rasiz).")

    elif action == "list_candidates":
        sql.execute("SELECT id, name FROM candidates ORDER BY id ASC")
        rows = sql.fetchall()
        if not rows:
            await c.message.answer("Nomzodlar yo‘q.")
        else:
            text = "<b>📃 Nomzodlar:</b>\n"
            for cid, name in rows:
                text += f"• <code>{cid}</code> — {name}\n"
            await c.message.answer(text)

    elif action == "set_timer":
        await AdminState.set_timer.set()
        await c.message.answer(
            "Taymer o‘rnatish (daqiqada).\n"
            "Masalan: <code>60</code> (60 daqiqa)\n\n"
            "⚠️ Taymer tugasa ovoz berish yopiladi."
        )

    elif action == "stop_timer":
        sql.execute("DELETE FROM settings WHERE key='end_time_utc'")
        db.commit()
        await c.message.answer("🛑 Taymer o‘chirildi. Ovoz berish ochiq (cheklanmagan).")

    elif action == "reset_votes":
        sql.execute("DELETE FROM votes")
        db.commit()
        await c.message.answer("🗑 Barcha ovozlar 0 qilindi.")

    elif action == "results":
        sql.execute("""
            SELECT c.name, COUNT(v.candidate_id) as cnt
            FROM candidates c
            LEFT JOIN votes v ON v.candidate_id = c.id
            GROUP BY c.id
            ORDER BY cnt DESC, c.id ASC
        """)
        rows = sql.fetchall()
        if not rows:
            await c.message.answer("Natija yo‘q (nomzod yoki ovoz yo‘q).")
        else:
            text = "<b>📊 Natijalar:</b>\n\n"
            total = 0
            for _, cnt in rows:
                total += cnt
            for name, cnt in rows:
                pct = (cnt / total * 100) if total else 0
                text += f"• {name}: <b>{cnt}</b> ({pct:.1f}%)\n"
            text += f"\n{remaining_time_text()}"
            await c.message.answer(text)

@dp.message_handler(state=AdminState.add_channel)
async def st_add_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    parts = m.text.strip().split()
    chat_id = parts[0]
    join_url = parts[1] if len(parts) > 1 else None

    try:
        sql.execute("INSERT INTO channels(chat_id, join_url) VALUES(?,?)", (chat_id, join_url))
        db.commit()
        await m.answer(f"✅ Kanal qo‘shildi: <code>{chat_id}</code>")
    except sqlite3.IntegrityError:
        await m.answer("❗ Bu kanal allaqachon ro‘yxatda bor.")
    except Exception:
        await m.answer("❌ Xatolik. Formatni tekshiring.")
    await state.finish()

@dp.message_handler(state=AdminState.remove_channel)
async def st_rm_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    chat_id = m.text.strip()
    sql.execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
    db.commit()
    await m.answer(f"✅ O‘chirildi (bo‘lgan bo‘lsa): <code>{chat_id}</code>")
    await state.finish()

@dp.message_handler(state=AdminState.add_candidate)
async def st_add_candidate(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    name = m.text.strip()
    if not name:
        await m.answer("❌ Bo‘sh nom bo‘maydi.")
        return
    sql.execute("INSERT INTO candidates(name) VALUES(?)", (name,))
    db.commit()
    await m.answer(f"✅ Nomzod qo‘shildi: <b>{name}</b>")
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
    sql.execute("DELETE FROM candidates WHERE id=?", (cid,))
    db.commit()
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
    sql.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES('end_time_utc', ?)",
        (end_time.isoformat(),)
    )
    db.commit()
    await m.answer(f"⏳ Taymer o‘rnatildi: <b>{mins}</b> daqiqa.\n{remaining_time_text()}")
    await state.finish()

# ----------------- RUN -----------------
if __name__ == "__main__":
    print("🚀 BOT STARTED, polling...")
    executor.start_polling(dp, skip_updates=True)
