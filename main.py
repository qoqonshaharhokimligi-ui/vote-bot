import os
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ----------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

ADMINS = [32257986]  # <-- admin user_id lar
UTC = timezone.utc

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

db_pool: Optional[asyncpg.Pool] = None

# FSM faqat kanal/taymer/o‘chirish uchun
class AdminState(StatesGroup):
    add_channel = State()
    remove_channel = State()
    remove_candidate = State()
    set_timer = State()

# FSMsiz bulk qo‘shish uchun “mode”
ADD_CANDIDATE_MODE = set()  # admin user_id lar


# ----------------- HELPERS -----------------
def is_admin(uid: int) -> bool:
    return uid in ADMINS

def now_utc() -> datetime:
    return datetime.now(UTC)

def pct_bar(pct: float, width: int = 10) -> str:
    filled = int(round((pct / 100.0) * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)

def parse_chat_id(raw: str):
    raw = raw.strip()
    if raw.startswith("@"):
        return raw
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw

async def db_fetch(query: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        return await conn.fetch(query, *args)

async def db_fetchrow(query: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(query, *args)

async def db_fetchval(query: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        return await conn.fetchval(query, *args)

async def db_execute(query: str, *args):
    assert db_pool is not None
    async with db_pool.acquire() as conn:
        return await conn.execute(query, *args)


# ----------------- SETTINGS / TIMER -----------------
async def get_setting(key: str) -> Optional[str]:
    return await db_fetchval("SELECT value FROM settings WHERE key=$1", key)

async def set_setting(key: str, value: Optional[str]) -> None:
    if value is None:
        await db_execute("DELETE FROM settings WHERE key=$1", key)
        return
    await db_execute("""
        INSERT INTO settings(key, value) VALUES($1, $2)
        ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """, key, value)

async def get_end_time() -> Optional[datetime]:
    v = await get_setting("end_time_utc")
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
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


# ----------------- SUBSCRIBE CHECK -----------------
async def is_subscribed(user_id: int) -> bool:
    rows = await db_fetch("SELECT chat_id FROM channels ORDER BY created_at DESC")
    if not rows:
        # Kanal qo‘shilmagan bo‘lsa majburiy obuna yo‘q
        return True

    for r in rows:
        chat_id = parse_chat_id(r["chat_id"])
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                return False
        except Exception:
            return False
    return True

async def subscribe_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    rows = await db_fetch("SELECT chat_id, join_url FROM channels ORDER BY created_at DESC")
    for r in rows:
        chat_id = r["chat_id"]
        join_url = r["join_url"]
        url = join_url
        if not url and isinstance(chat_id, str) and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            kb.add(InlineKeyboardButton(text=f"➕ Obuna bo‘lish: {chat_id}", url=url))

    kb.add(InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_sub"))
    return kb


# ----------------- UI: ADMIN KEYBOARD -----------------
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
        InlineKeyboardButton("🛑 Taymer stop", callback_data="a:timer_stop"),
    )
    kb.add(
        InlineKeyboardButton("📊 Natijalar", callback_data="a:results"),
        InlineKeyboardButton("🗑 Ovozlarni 0 qilish", callback_data="a:reset_votes"),
    )
    return kb


# ----------------- UI: VOTE KEYBOARD (REAL TIME) -----------------
async def vote_kb(disabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    rows = await db_fetch("""
        SELECT c.id, c.name, COUNT(v.user_id) AS cnt
        FROM candidates c
        LEFT JOIN votes v ON v.candidate_id = c.id
        GROUP BY c.id, c.name
        ORDER BY c.id ASC
    """)
    if not rows:
        kb.add(InlineKeyboardButton("Nomzodlar hali yo‘q", callback_data="noop"))
        return kb

    total = sum(int(r["cnt"]) for r in rows)

    for idx, r in enumerate(rows, start=1):
        cid = int(r["id"])
        name = str(r["name"])
        cnt = int(r["cnt"])
        pct = (cnt * 100.0 / total) if total else 0.0

        text = f"{idx}. {name} — {cnt} ({pct:.0f}%)"
        cb = "noop" if disabled else f"v:{cid}"
        kb.add(InlineKeyboardButton(text, callback_data=cb))

    return kb


async def voting_message_text() -> str:
    open_state = "✅ Ovoz berish: <b>ochiq</b>" if await voting_is_open() else "🚫 Ovoz berish: <b>yopiq</b>"
    return (
        "🗳 <b>Ovoz berish</b>\n"
        "Nomzodni tanlang (real-time):\n\n"
        f"{await remaining_time_text()}\n"
        f"{open_state}"
    )


# ----------------- RESULTS / EXPORT -----------------
async def build_results_text() -> str:
    rows = await db_fetch("""
        SELECT c.id, c.name, COUNT(v.user_id) AS cnt
        FROM candidates c
        LEFT JOIN votes v ON v.candidate_id = c.id
        GROUP BY c.id, c.name
        ORDER BY cnt DESC, c.id ASC
    """)
    total = sum(int(r["cnt"]) for r in rows) if rows else 0

    if not rows:
        return "Nomzodlar yo‘q."

    lines = []
    for i, r in enumerate(rows, start=1):
        name = str(r["name"])
        cnt = int(r["cnt"])
        pct = (cnt * 100.0 / total) if total else 0.0
        lines.append(f"{i}) <b>{name}</b>: {cnt} ta ({pct:.1f}%)\n  {pct_bar(pct, 14)}")

    return (
        "📊 <b>Natijalar (real-time)</b>\n\n"
        f"🧮 Umumiy ovoz: <b>{total}</b>\n\n" +
        "\n".join(lines)
    )

async def export_votes_csv_bytes() -> bytes:
    rows = await db_fetch("""
        SELECT v.user_id, v.candidate_id, c.name AS candidate_name, v.voted_at
        FROM votes v
        JOIN candidates c ON c.id = v.candidate_id
        ORDER BY v.voted_at DESC
    """)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user_id", "candidate_id", "candidate_name", "voted_at"])
    for r in rows:
        writer.writerow([r["user_id"], r["candidate_id"], r["candidate_name"], r["voted_at"].isoformat()])
    return output.getvalue().encode("utf-8")


# ----------------- USER FLOW -----------------
@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message):
    if not await is_subscribed(m.from_user.id):
        await m.answer("Davom etish uchun quyidagi kanallarga obuna bo‘ling:", reply_markup=await subscribe_kb())
        return

    if not await voting_is_open():
        await m.answer(f"🚫 Ovoz berish yopiq.\n\n{await remaining_time_text()}")
        return

    await m.answer(await voting_message_text(), reply_markup=await vote_kb(disabled=False))


@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check_sub(c: types.CallbackQuery):
    ok = await is_subscribed(c.from_user.id)
    if not ok:
        await c.answer("Hali obuna emassiz", show_alert=True)
        return
    await c.answer("✅ Obuna tasdiqlandi", show_alert=True)
    await cmd_start(c.message)


@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(c: types.CallbackQuery):
    await c.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("v:"))
async def cb_vote(c: types.CallbackQuery):
    # 1) Obuna bo‘lmasa ovoz yo‘q (QAT’IY)
    if not await is_subscribed(c.from_user.id):
        await c.answer("Avval kanallarga obuna bo‘ling", show_alert=True)
        await c.message.answer("Davom etish uchun quyidagi kanallarga obuna bo‘ling:", reply_markup=await subscribe_kb())
        return

    # 2) Taymer tugagan bo‘lsa
    if not await voting_is_open():
        await c.answer("🚫 Ovoz berish yopiq", show_alert=True)
        try:
            await c.message.edit_reply_markup(reply_markup=await vote_kb(disabled=True))
        except Exception:
            pass
        return

    try:
        cid = int(c.data.split(":")[1])
    except Exception:
        await c.answer("Xato", show_alert=True)
        return

    # 3) 1 user = 1 vote (almashtirishga ruxsat)
    await db_execute("""
        INSERT INTO votes(user_id, candidate_id)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET candidate_id=EXCLUDED.candidate_id, voted_at=NOW()
    """, c.from_user.id, cid)

    # 4) “Otib ketganday” — shu xabarni yangilaymiz
    await c.answer("✅ Ovozingiz qabul qilindi", show_alert=False)

    try:
        await c.message.edit_text(await voting_message_text(), reply_markup=await vote_kb(disabled=False))
    except Exception:
        try:
            await c.message.edit_reply_markup(reply_markup=await vote_kb(disabled=False))
        except Exception:
            pass


# ----------------- ADMIN COMMANDS -----------------
@dp.message_handler(commands=["admin"])
async def cmd_admin(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⚙️ <b>Admin panel</b>", reply_markup=admin_kb())

@dp.message_handler(commands=["results"])
async def cmd_results(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(await build_results_text())

@dp.message_handler(commands=["reset_votes"])
async def cmd_reset_votes(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await db_execute("TRUNCATE votes")
    await m.answer("🗑 Ovozlar 0 qilindi.")

@dp.message_handler(commands=["export"])
async def cmd_export(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    data = await export_votes_csv_bytes()
    f = types.InputFile(io.BytesIO(data), filename="votes.csv")
    await m.answer_document(f, caption="📤 votes.csv")


# ----------------- ADMIN CALLBACKS -----------------
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
        rows = await db_fetch("SELECT chat_id, join_url FROM channels ORDER BY created_at DESC")
        if not rows:
            await c.message.answer("Kanallar yo‘q.")
        else:
            lines = []
            for r in rows:
                lines.append(f"• <code>{r['chat_id']}</code>" + (f" — {r['join_url']}" if r["join_url"] else ""))
            await c.message.answer("📃 <b>Kanallar</b>\n\n" + "\n".join(lines))

    elif action == "add_candidate":
        # FSMsiz bulk qo‘shish
        ADD_CANDIDATE_MODE.add(c.from_user.id)
        await c.message.answer(
            "📝 Nomzod(lar)ni yuboring.\n"
            "➡️ Har qatorda bittadan.\n\n"
            "Masalan:\n"
            "Ali\nVali\nGuli\n\n"
            "❌ Bekor qilish: /cancel"
        )

    elif action == "rm_candidate":
        await AdminState.remove_candidate.set()
        await c.message.answer(
            "O‘chirish uchun yuboring:\n"
            "• ID (masalan: <code>7</code>)\n"
            "yoki\n"
            "• Tartib raqam (1/2/3…)\n"
            "yoki\n"
            "• Nomzod nomi (masalan: <code>Ali</code>)"
        )

    elif action == "list_candidates":
        rows = await db_fetch("SELECT id, name FROM candidates ORDER BY id ASC")
        if not rows:
            await c.message.answer("Nomzodlar yo‘q.")
        else:
            txt = "\n".join([f"{i}. {r['name']} (ID: {r['id']})" for i, r in enumerate(rows, start=1)])
            await c.message.answer("📃 <b>Nomzodlar</b>\n\n" + txt)

    elif action == "set_timer":
        await AdminState.set_timer.set()
        await c.message.answer("Taymer o‘rnatish (daqiqada). Masalan: <code>60</code>")

    elif action == "timer_stop":
        # Darhol yopish
        await set_setting("end_time_utc", now_utc().isoformat())
        await c.message.answer("🛑 Taymer to‘xtatildi. Ovoz berish yopildi.")

    elif action == "results":
        await c.message.answer(await build_results_text())

    elif action == "reset_votes":
        await db_execute("TRUNCATE votes")
        await c.message.answer("🗑 Ovozlar 0 qilindi.")


# ----------------- ADMIN: BULK ADD NOMZOD (FSMsiz) -----------------
@dp.message_handler(lambda m: m.from_user and m.from_user.id in ADD_CANDIDATE_MODE)
async def add_candidates_auto(m: types.Message):
    if not is_admin(m.from_user.id):
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        return

    text = (m.text or "").strip()
    if text.lower() == "/cancel":
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        await m.answer("❌ Nomzod qo‘shish bekor qilindi.")
        return

    names = [x.strip() for x in text.split("\n") if x.strip()]
    if not names:
        await m.answer("⚠️ Nomzod nomlarini yuboring (har qatorda bittadan).")
        return

    added = 0
    skipped = 0

    async with db_pool.acquire() as conn:
        for name in names:
            exists = await conn.fetchval(
                "SELECT 1 FROM candidates WHERE LOWER(name)=LOWER($1)",
                name
            )
            if exists:
                skipped += 1
                continue
            await conn.execute("INSERT INTO candidates(name) VALUES($1)", name)
            added += 1

    ADD_CANDIDATE_MODE.discard(m.from_user.id)
    await m.answer(f"✅ Qo‘shildi: {added}\n⚠️ Takror bo‘lgani uchun o‘tkazib yuborildi: {skipped}", reply_markup=admin_kb())


@dp.message_handler(commands=["cancel"])
async def cancel_any(m: types.Message):
    if m.from_user and m.from_user.id in ADD_CANDIDATE_MODE:
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        await m.answer("❌ Bekor qilindi.")
        return


# ----------------- ADMIN: ADD/REMOVE CHANNEL (FSM) -----------------
@dp.message_handler(state=AdminState.add_channel)
async def st_add_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    parts = m.text.strip().split()
    chat_id = parts[0]
    join_url = parts[1] if len(parts) > 1 else None

    await db_execute("""
        INSERT INTO channels(chat_id, join_url)
        VALUES($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET join_url=EXCLUDED.join_url
    """, chat_id, join_url)

    await state.finish()
    await m.answer(f"✅ Kanal qo‘shildi: <b>{chat_id}</b>", reply_markup=admin_kb())

@dp.message_handler(state=AdminState.remove_channel)
async def st_rm_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    chat_id = m.text.strip().split()[0]
    await db_execute("DELETE FROM channels WHERE chat_id=$1", chat_id)

    await state.finish()
    await m.answer(f"✅ Kanal o‘chirildi: <b>{chat_id}</b>", reply_markup=admin_kb())


# ----------------- ADMIN: REMOVE NOMZOD (ID yoki tartib raqam) -----------------
@dp.message_handler(state=AdminState.remove_candidate)
async def st_rm_candidate(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    raw = m.text.strip()

    # Raqam bo‘lsa: avval ID deb urinadi, bo‘lmasa tartib raqami deb oladi
    if raw.isdigit():
        n = int(raw)

        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM candidates WHERE id=$1", n)
            deleted = int(res.split()[-1])
            if deleted == 1:
                await state.finish()
                await m.answer(f"✅ Nomzod o‘chirildi: ID <b>{n}</b>", reply_markup=admin_kb())
                return

            row = await conn.fetchrow("""
                SELECT id, name
                FROM candidates
                ORDER BY id ASC
                OFFSET $1
                LIMIT 1
            """, n - 1)

            if not row:
                await state.finish()
                await m.answer("❌ Bunday tartib raqamdagi nomzod topilmadi.", reply_markup=admin_kb())
                return

            cid = int(row["id"])
            name = str(row["name"])
            await conn.execute("DELETE FROM candidates WHERE id=$1", cid)

        await state.finish()
        await m.answer(f"✅ Nomzod o‘chirildi: <b>{n}. {name}</b> (ID: {cid})", reply_markup=admin_kb())
        return

    # Nom bo‘yicha o‘chirish
    res = await db_execute("DELETE FROM candidates WHERE LOWER(name)=LOWER($1)", raw)
    deleted = int(res.split()[-1])

    await state.finish()
    if deleted:
        await m.answer(f"✅ Nomzod o‘chirildi: <b>{raw}</b>", reply_markup=admin_kb())
    else:
        await m.answer("❌ Nomzod topilmadi (nomni tekshiring).", reply_markup=admin_kb())


# ----------------- ADMIN: SET TIMER (FSM) -----------------
@dp.message_handler(state=AdminState.set_timer)
async def st_set_timer(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    raw = m.text.strip()
    if not raw.isdigit():
        await m.answer("Faqat raqam yuboring. Masalan: <code>60</code>")
        return

    minutes = int(raw)
    if minutes <= 0:
        await m.answer("0 dan katta bo‘lsin.")
        return

    end_time = now_utc() + timedelta(minutes=minutes)
    await set_setting("end_time_utc", end_time.isoformat())

    await state.finish()
    await m.answer(f"✅ Taymer o‘rnatildi: <b>{minutes} daqiqa</b>\n{await remaining_time_text()}", reply_markup=admin_kb())


# ----------------- DB INIT -----------------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS channels(
                chat_id TEXT PRIMARY KEY,
                join_url TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates(
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS votes(
                user_id BIGINT PRIMARY KEY,
                candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                voted_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)


# ----------------- STARTUP / SHUTDOWN -----------------
async def on_startup(_dp: Dispatcher):
    await init_db()
    print("DB: POSTGRES | READY")
    print("BOT STARTED")

async def on_shutdown(_dp: Dispatcher):
    global db_pool
    if db_pool:
        await db_pool.close()
        db_pool = None


if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
