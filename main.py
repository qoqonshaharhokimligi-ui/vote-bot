import os
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ----------------- CONFIG -----------------
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable topilmadi. PowerShell: $env:BOT_TOKEN='...'")

ADMINS = [32257986]  # <-- shu yerga o'zingizning user_id ni yozing (@userinfobot)

UTC = timezone.utc

bot = Bot(API_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ----------------- DB -----------------
db = sqlite3.connect("db.sqlite", check_same_thread=False)
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS channels(
    chat_id TEXT PRIMARY KEY,     -- '@kanal' yoki '-100....'
    join_url TEXT                 -- ixtiyoriy (private kanal uchun tavsiya)
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
        return "⏳ Taymer: o‘rnatilmagan (ovoz berish ochiq)"
    delta = end_time - now_utc()
    if delta.total_seconds() <= 0:
        return "⏳ Taymer: tugagan (ovoz berish yopiq)"
    mins = int(delta.total_seconds() // 60)
    secs = int(delta.total_seconds() % 60)
    return f"⏳ Qolgan vaqt: <b>{mins:02d}:{secs:02d}</b>"

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
            # bot kanalga admin bo'lmasa yoki chat_id noto'g'ri bo'lsa ham shu yerga tushishi mumkin
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
        kb.add(InlineKeyboardButton("⛔ Kandidatlar yo‘q (admin qo‘shadi)", callback_data="noop"))
        return kb

    for cid, name, cnt in rows:
        cb = "noop" if disabled else f"vote:{cid}"
        kb.add(InlineKeyboardButton(text=f"{name} — {cnt}", callback_data=cb))

    return kb

def total_votes() -> int:
    sql.execute("SELECT COUNT(*) FROM votes")
    return int(sql.fetchone()[0])

def voting_message_text() -> str:
    open_state = "✅ Ovoz berish: <b>ochiq</b>" if voting_is_open() else "⛔ Ovoz berish: <b>yopiq</b>"
    return (
        "🗳 <b>Ovoz berish</b>\n\n"
        "Kerakli kandidatni tanlang:\n\n"
        f"🧮 Jami ovozlar: <b>{total_votes()}</b>\n"
        f"{remaining_time_text()}\n"
        f"{open_state}"
    )

def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Kanal", callback_data="a:add_channel"),
        InlineKeyboardButton("➖ Kanal", callback_data="a:rm_channel"),
    )
    kb.add(
        InlineKeyboardButton("📃 Kanallar", callback_data="a:list_channels"),
        InlineKeyboardButton("📃 Kandidatlar", callback_data="a:list_candidates"),
    )
    kb.add(
        InlineKeyboardButton("➕ Kandidat", callback_data="a:add_candidate"),
        InlineKeyboardButton("➖ Kandidat", callback_data="a:rm_candidate"),
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
    args = m.get_args().strip().lower() if m.get_args() else ""
    if args == "vote":
        await m.answer(voting_message_text(), reply_markup=live_vote_kb(disabled=not voting_is_open()))
        return

    await m.answer(
        "Assalomu alaykum!\n\n"
        "Ovoz berish uchun quyidagi tugmani bosing:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🗳 Ovoz berish", callback_data="open_vote")
        )
    )

@dp.callback_query_handler(lambda c: c.data == "open_vote")
async def cb_open_vote(c: types.CallbackQuery):
    await c.answer()
    await c.message.answer(voting_message_text(), reply_markup=live_vote_kb(disabled=not voting_is_open()))

@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check_sub(c: types.CallbackQuery):
    ok = await is_subscribed(c.from_user.id)
    if ok:
        await c.answer("✅ Obuna tasdiqlandi!", show_alert=True)
        await c.message.answer(voting_message_text(), reply_markup=live_vote_kb(disabled=not voting_is_open()))
    else:
        await c.answer("❗ Avval barcha kanallarga obuna bo‘ling.", show_alert=True)

@dp.callback_query_handler(lambda c: c.data.startswith("vote:"))
async def cb_vote(c: types.CallbackQuery):
    if not voting_is_open():
        await c.answer("⛔ Ovoz berish yakunlangan.", show_alert=True)
        try:
            await c.message.edit_text(voting_message_text(), reply_markup=live_vote_kb(disabled=True))
        except Exception:
            pass
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
        await c.answer("Kandidat topilmadi", show_alert=True)
        return

    try:
        sql.execute(
            "INSERT INTO votes(user_id, candidate_id, ts) VALUES(?,?,?)",
            (c.from_user.id, cid, now_utc().isoformat())
        )
        db.commit()
        await c.answer("✅ Ovoz qabul qilindi!", show_alert=True)
        await c.message.edit_text(voting_message_text(), reply_markup=live_vote_kb(disabled=False))
    except sqlite3.IntegrityError:
        await c.answer("❗ Siz allaqachon ovoz bergansiz.", show_alert=True)
        try:
            await c.message.edit_text(voting_message_text(), reply_markup=live_vote_kb(disabled=False))
        except Exception:
            pass

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
            "Ixtiyoriy: link ham qo‘shing (private uchun tavsiya):\n"
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
        await c.message.answer("Kandidat nomini yuboring (tugmada qanday chiqishi kerak bo‘lsa shunday).")

    elif action == "rm_candidate":
        await AdminState.remove_candidate.set()
        await c.message.answer("Kandidat ID’sini yuboring (ID ni «📃 Kandidatlar»dan ko‘rasiz).")

    elif action == "list_candidates":
        sql.execute("SELECT id, name FROM candidates ORDER BY id ASC")
        rows = sql.fetchall()
        if not rows:
            await c.message.answer("Kandidatlar yo‘q.")
        else:
            text = "<b>📃 Kandidatlar:</b>\n"
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
        total = sum(r[1] for r in rows)
        if not rows:
            await c.message.answer("Natija yo‘q (kandidat yoki ovoz yo‘q).")
        else:
            text = "<b>📊 Natijalar:</b>\n\n"
            for name, cnt in rows:
                pct = (cnt / total * 100) if total else 0
                text += f"• {name}: <b>{cnt}</b> ({pct:.1f}%)\n"
            text += f"\n🧮 Jami: <b>{total}</b>\n{remaining_time_text()}"
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
        await m.answer("❌ Bo‘sh nom bo‘lmaydi.")
        return
    sql.execute("INSERT INTO candidates(name) VALUES(?)", (name,))
    db.commit()
    await m.answer(f"✅ Kandidat qo‘shildi: <b>{name}</b>")
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
    await m.answer(f"✅ Kandidat o‘chirildi (bo‘lgan bo‘lsa): <code>{cid}</code>")
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
    executor.start_polling(dp, skip_updates=True)

@dp.message_handler()
async def debug_all(m: types.Message):
    print("GOT MESSAGE:", m.text)
    await m.answer("Bot ishlayapti ✅")

