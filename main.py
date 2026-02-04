import os
import csv
import io
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

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
    raise RuntimeError("BOT_TOKEN environment variable topilmadi")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable topilmadi")

ADMINS = [32257986]  # <-- o'zingizniki

UTC = timezone.utc

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

db_pool: Optional[asyncpg.Pool] = None

# FSM faqat remove/timer/channel uchun
class AdminState(StatesGroup):
    add_channel = State()
    remove_channel = State()
    remove_candidate = State()
    set_timer = State()

# FSMsiz bulk add uchun "mode"
ADD_CANDIDATE_MODE = set()  # admin user_id lar


# ----------------- DB HELPERS -----------------
def is_admin(uid: int) -> bool:
    return uid in ADMINS

def now_utc() -> datetime:
    return datetime.now(UTC)

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
        return "⏳ Таймер: ўрнатилмаган (овоз бериш очиқ)"
    delta = end_time - now_utc()
    if delta.total_seconds() <= 0:
        return "⏳ Таймер: тугаган (овоз бериш ёпиқ)"
    mins = int(delta.total_seconds() // 60)
    secs = int(delta.total_seconds() % 60)
    return f"⏳ Қолган вақт: <b>{mins:02d}:{secs:02d}</b>"


# ----------------- CHANNEL NORMALIZE -----------------
def normalize_channel_input(raw: str) -> Tuple[str, Optional[str]]:
    """
    Accepts:
      @username
      https://t.me/username
      t.me/username
      -100123... (private)
      -100123... https://t.me/+invite
    Returns: (chat_id, join_url)
    """
    parts = raw.strip().split()
    first = parts[0].strip()
    join_url = parts[1].strip() if len(parts) > 1 else None

    # URL -> @username
    m = re.search(r"(?:https?://)?t\.me/([A-Za-z0-9_]+)/?$", first)
    if m:
        username = m.group(1)
        chat_id = f"@{username}"
        if not join_url:
            join_url = f"https://t.me/{username}"
        return chat_id, join_url

    if first.startswith("@"):
        chat_id = first
        if not join_url:
            join_url = f"https://t.me/{first.lstrip('@')}"
        return chat_id, join_url

    if re.fullmatch(r"-100\d{5,}", first):
        # private kanal: join_url bo‘lsa yaxshi
        return first, join_url

    raise ValueError("Канал формати нотўғри")


# ----------------- SUBSCRIBE CHECK -----------------
async def get_channels() -> List[Tuple[str, Optional[str]]]:
    rows = await db_fetch("SELECT chat_id, join_url FROM channels ORDER BY created_at DESC")
    return [(str(r["chat_id"]), (str(r["join_url"]) if r["join_url"] else None)) for r in rows]

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
            # bot kanalga admin bo‘lmasa yoki chat_id noto‘g‘ri bo‘lsa
            return False
    return True

def subscribe_kb(channels: List[Tuple[str, Optional[str]]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    has_any = False
    for chat_id, join_url in channels:
        url = join_url
        if not url and chat_id.startswith("@"):
            url = f"https://t.me/{chat_id.lstrip('@')}"
        if url:
            has_any = True
            kb.add(InlineKeyboardButton(text=f"➕ Обуна бўлиш: {chat_id}", url=url))

    if not has_any:
        kb.add(InlineKeyboardButton("⚠️ Канал линклари йўқ (админ қўшсин)", callback_data="noop"))

    kb.add(InlineKeyboardButton(text="✅ Текшириш", callback_data="check_sub"))
    return kb


# ----------------- VOTE UI (REAL-TIME COUNTS) -----------------
async def candidates_with_counts() -> List[Tuple[int, str, int]]:
    rows = await db_fetch("""
        SELECT c.id, c.name, COUNT(v.user_id) AS cnt
        FROM candidates c
        LEFT JOIN votes v ON v.candidate_id = c.id
        GROUP BY c.id, c.name
        ORDER BY c.id ASC
    """)
    return [(int(r["id"]), str(r["name"]), int(r["cnt"])) for r in rows]

async def total_votes() -> int:
    v = await db_fetchval("SELECT COUNT(*) FROM votes")
    return int(v or 0)

def safe_btn_text(s: str, max_len: int = 60) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= max_len else (s[: max_len - 1] + "…")

async def vote_kb(disabled: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    rows = await candidates_with_counts()
    total = sum(cnt for _cid, _n, cnt in rows)

    if not rows:
        kb.add(InlineKeyboardButton("⛔ Номзодлар йўқ (админ қўшади)", callback_data="noop"))
        return kb

    for idx, (cid, name, cnt) in enumerate(rows, start=1):
        pct = int((cnt / total) * 100) if total > 0 else 0
        text = safe_btn_text(f"{idx}. {name} | {cnt} та | {pct}%")
        cb = "noop" if disabled else f"v:{cid}"
        kb.add(InlineKeyboardButton(text=text, callback_data=cb))

    return kb

async def voting_message_text() -> str:
    open_state = "✅ Овоз бериш: <b>очиқ</b>" if await voting_is_open() else "🚫 Овоз бериш: <b>ёпиқ</b>"
    return (
        "🗳 <b>Овоз бериш</b>\n"
        "Номзодни танланг (real-time):\n\n"
        f"🧮 Жами овоз: <b>{await total_votes()}</b>\n"
        f"{await remaining_time_text()}\n"
        f"{open_state}"
    )


# ----------------- RESULTS AS BUTTONS (rank+name+votes+%) -----------------
async def results_text_and_buttons() -> Tuple[str, InlineKeyboardMarkup]:
    rows = await candidates_with_counts()
    total = sum(cnt for _cid, _n, cnt in rows)

    if not rows:
        text = "📊 <b>Натижалар</b>\n\n❌ Номзодлар қўшилмаган."
        return text, InlineKeyboardMarkup().add(InlineKeyboardButton("↩️ Админ", callback_data="a:back"))

    # sort by votes desc
    sorted_rows = sorted(rows, key=lambda x: (-x[2], x[0]))

    if total == 0:
        head = (
            "📊 <b>Натижалар</b>\n"
            f"🧮 Жами овоз: <b>0</b>\n\n"
            "Ҳозирча овоз берилмаган. Номзодлар кесимида 0 натижа кўрсатилмоқда:"
        )
    else:
        head = (
            "📊 <b>Натижалар</b>\n"
            f"🧮 Жами овоз: <b>{total}</b>\n\n"
            "Номзодлар кесимида натижалар:"
        )

    # deep link uchun bot username
    me = await bot.get_me()
    bot_username = me.username

    kb = InlineKeyboardMarkup(row_width=1)

    for rank, (cid, name, cnt) in enumerate(sorted_rows, start=1):
        pct = int((cnt / total) * 100) if total > 0 else 0
        label = safe_btn_text(f"{rank}. {name} | {cnt} та | {pct}%")

        if bot_username:
            url = f"https://t.me/{bot_username}?start=c{cid}"
            kb.add(InlineKeyboardButton(text=label, url=url))
        else:
            kb.add(InlineKeyboardButton(text=label, callback_data=f"open_c:{cid}"))

    # actions
    kb.add(InlineKeyboardButton("🔄 Янгилаш", callback_data="refresh_results"))
    kb.add(InlineKeyboardButton("🗳 Овоз бериш", callback_data="open_vote"))

    return head, kb


# ----------------- ADMIN PANEL -----------------
def admin_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Канал", callback_data="a:add_channel"),
        InlineKeyboardButton("➖ Канал", callback_data="a:rm_channel"),
    )
    kb.add(
        InlineKeyboardButton("📃 Каналлар", callback_data="a:list_channels"),
        InlineKeyboardButton("📃 Номзодлар", callback_data="a:list_candidates"),
    )
    kb.add(
        InlineKeyboardButton("➕ Номзод", callback_data="a:add_candidate"),
        InlineKeyboardButton("➖ Номзод", callback_data="a:rm_candidate"),
    )
    kb.add(
        InlineKeyboardButton("⏳ Таймер (daq)", callback_data="a:set_timer"),
        InlineKeyboardButton("🛑 Таймер stop", callback_data="a:timer_stop"),
    )
    kb.add(
        InlineKeyboardButton("📊 Натижалар", callback_data="a:results"),
        InlineKeyboardButton("🗑 Овозларни 0 қилиш", callback_data="a:reset_votes"),
    )
    kb.add(
        InlineKeyboardButton("📤 Export CSV", callback_data="a:export_csv"),
        InlineKeyboardButton("♻️ Back", callback_data="a:back"),
    )
    return kb


# ----------------- START / SUBSCRIBE FLOW -----------------
@dp.message_handler(commands=["start"])
async def cmd_start(m: types.Message):
    args = (m.get_args() or "").strip()

    # deep link: /start c<ID>
    if args.startswith("c") and args[1:].isdigit():
        cid = int(args[1:])

        # obuna tekshiruv (admin ham, user ham)
        channels = await get_channels()
        if not await is_subscribed(m.from_user.id):
            await m.answer(
                "🔒 Давом этиш учун қуйидаги каналларга обуна бўлинг ва <b>✅ Текшириш</b>ни босинг:",
                reply_markup=subscribe_kb(channels)
            )
            return

        if not await voting_is_open():
            await m.answer(f"🚫 Овоз бериш ёпиқ.\n\n{await remaining_time_text()}")
            return

        # candidate exists?
        row = await db_fetchrow("SELECT id, name FROM candidates WHERE id=$1", cid)
        if not row:
            await m.answer("❌ Номзод топилмади.")
            return

        # show voting with highlight button
        kb = InlineKeyboardMarkup(row_width=1)
        kb.add(InlineKeyboardButton(f"✅ {row['name']} учун овоз бериш", callback_data=f"v:{cid}"))
        kb.add(InlineKeyboardButton("⬅️ Барча номзодлар", callback_data="open_vote"))
        await m.answer("🗳 <b>Номзодга овоз бериш</b>\nТасдиқланг:", reply_markup=kb)
        return

    # default start:
    channels = await get_channels()
    if not await is_subscribed(m.from_user.id):
        await m.answer(
            "🔒 Давом этиш учун қуйидаги каналларга обуна бўлинг ва <b>✅ Текшириш</b>ни босинг:",
            reply_markup=subscribe_kb(channels)
        )
        return

    if not await voting_is_open():
        await m.answer(f"🚫 Овоз бериш ёпиқ.\n\n{await remaining_time_text()}")
        return

    await m.answer(await voting_message_text(), reply_markup=await vote_kb(disabled=False))


@dp.callback_query_handler(lambda c: c.data == "check_sub")
async def cb_check_sub(c: types.CallbackQuery):
    ok = await is_subscribed(c.from_user.id)
    if not ok:
        await c.answer("Ҳали обуна эмассиз (бот каналларда admin бўлиши керак)", show_alert=True)
        return
    await c.answer("✅ Обуна тасдиқланди", show_alert=True)
    # show voting
    if not await voting_is_open():
        await c.message.answer(f"🚫 Овоз бериш ёпиқ.\n\n{await remaining_time_text()}")
        return
    await c.message.answer(await voting_message_text(), reply_markup=await vote_kb(disabled=False))


@dp.callback_query_handler(lambda c: c.data == "open_vote")
async def cb_open_vote(c: types.CallbackQuery):
    await c.answer()
    channels = await get_channels()
    if not await is_subscribed(c.from_user.id):
        await c.message.answer(
            "🔒 Овоз бериш учун аввало каналларга обуна бўлинг:",
            reply_markup=subscribe_kb(channels)
        )
        return

    if not await voting_is_open():
        await c.message.answer(f"🚫 Овоз бериш ёпиқ.\n\n{await remaining_time_text()}")
        return

    await c.message.answer(await voting_message_text(), reply_markup=await vote_kb(disabled=False))


@dp.callback_query_handler(lambda c: c.data == "noop")
async def cb_noop(c: types.CallbackQuery):
    await c.answer()


# ----------------- VOTE HANDLER -----------------
@dp.callback_query_handler(lambda c: c.data.startswith("v:"))
async def cb_vote(c: types.CallbackQuery):
    # obuna shart (admin ham, user ham)
    channels = await get_channels()
    if not await is_subscribed(c.from_user.id):
        await c.answer("Аввало каналларга обуна бўлинг", show_alert=True)
        await c.message.answer(
            "🔒 Давом этиш учун қуйидаги каналларга обуна бўлинг ва <b>✅ Текшириш</b>ни босинг:",
            reply_markup=subscribe_kb(channels)
        )
        return

    if not await voting_is_open():
        await c.answer("🚫 Овоз бериш ёпиқ", show_alert=True)
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

    # candidate exists?
    exists = await db_fetchval("SELECT 1 FROM candidates WHERE id=$1", cid)
    if not exists:
        await c.answer("❌ Номзод топилмади", show_alert=True)
        return

    # 1 user = 1 vote (almashtirishga ruxsat: UPDATE)
    await db_execute("""
        INSERT INTO votes(user_id, candidate_id)
        VALUES ($1, $2)
        ON CONFLICT (user_id)
        DO UPDATE SET candidate_id=EXCLUDED.candidate_id, voted_at=NOW()
    """, c.from_user.id, cid)

    await c.answer("✅ Овозингиз қабул қилинди", show_alert=False)

    # real-time update same message
    try:
        await c.message.edit_text(await voting_message_text(), reply_markup=await vote_kb(disabled=False))
    except Exception:
        try:
            await c.message.edit_reply_markup(reply_markup=await vote_kb(disabled=False))
        except Exception:
            pass


# ----------------- RESULTS: refresh + open candidate fallback -----------------
@dp.callback_query_handler(lambda c: c.data == "refresh_results")
async def cb_refresh_results(c: types.CallbackQuery):
    await c.answer("Янгиланди")
    text, kb = await results_text_and_buttons()
    try:
        await c.message.edit_text(text, reply_markup=kb)
    except Exception:
        await c.message.answer(text, reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data.startswith("open_c:"))
async def cb_open_candidate_fallback(c: types.CallbackQuery):
    await c.answer()
    cid = int(c.data.split(":")[1])
    await c.message.answer(f"/start c{cid}")


# ----------------- ADMIN COMMANDS -----------------
@dp.message_handler(commands=["admin"])
async def cmd_admin(m: types.Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⚙️ <b>Админ панел</b>", reply_markup=admin_kb())


# ----------------- ADMIN CALLBACKS -----------------
@dp.callback_query_handler(lambda c: c.data.startswith("a:"))
async def cb_admin_actions(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Кириш йўқ", show_alert=True)
        return

    action = c.data.split(":", 1)[1]
    await c.answer()

    if action == "back":
        await c.message.answer("⚙️ <b>Админ панел</b>", reply_markup=admin_kb())

    elif action == "add_channel":
        await AdminState.add_channel.set()
        await c.message.answer(
            "Канал қўшиш.\n\n"
            "Юборинг:\n"
            "• <b>@publickanal</b>\n"
            "ёки\n"
            "• <b>https://t.me/publickanal</b>\n"
            "ёки\n"
            "• <b>-1001234567890</b> (private)\n\n"
            "Private учун invite link қўшинг:\n"
            "<code>-100123... https://t.me/+invite</code>"
        )

    elif action == "rm_channel":
        await AdminState.remove_channel.set()
        await c.message.answer("Ўчириш учун канални юборинг: <b>@username</b> ёки <b>https://t.me/username</b> ёки <b>-100...</b>")

    elif action == "list_channels":
        rows = await get_channels()
        if not rows:
            await c.message.answer("Каналлар йўқ.")
        else:
            lines = []
            for chat_id, url in rows:
                lines.append(f"• <code>{chat_id}</code>" + (f" — {url}" if url else ""))
            await c.message.answer("📃 <b>Каналлар</b>\n\n" + "\n".join(lines))

    elif action == "add_candidate":
        # FSMsiz bulk add
        ADD_CANDIDATE_MODE.add(c.from_user.id)
        await c.message.answer(
            "📝 Номзод(лар)ни юборинг (har qatorda bittadan).\n\n"
            "Мисол:\n"
            "<code>Давронбек МФЙ\nШалдирамоқ МФЙ\nТолзор МФЙ</code>\n\n"
            "❌ Бекор қилиш: /cancel"
        )

    elif action == "rm_candidate":
        await AdminState.remove_candidate.set()
        await c.message.answer(
            "Ўчириш учун юборинг:\n"
            "• ID (масалан: <code>7</code>)\n"
            "ёки\n"
            "• Тартиб рақам (1/2/3…)\n"
            "ёки\n"
            "• Номзод номи (масалан: <code>Ali</code>)"
        )

    elif action == "list_candidates":
        rows = await db_fetch("SELECT id, name FROM candidates ORDER BY id ASC")
        if not rows:
            await c.message.answer("Номзодлар йўқ.")
        else:
            txt = "\n".join([f"{i}. {r['name']} (ID: {r['id']})" for i, r in enumerate(rows, start=1)])
            await c.message.answer("📃 <b>Номзодлар</b>\n\n" + txt)

    elif action == "set_timer":
        await AdminState.set_timer.set()
        await c.message.answer("Таймер ўрнатиш (daq). Масалан: <code>60</code>")

    elif action == "timer_stop":
        await set_setting("end_time_utc", now_utc().isoformat())
        await c.message.answer("🛑 Таймер тўхтатилди. Овоз бериш ёпилди.")

    elif action == "reset_votes":
        await db_execute("TRUNCATE votes")
        await c.message.answer("🗑 Овозлар 0 қилинди.")

    elif action == "export_csv":
        # export votes.csv
        rows = await db_fetch("""
            SELECT v.user_id, v.candidate_id, c.name AS candidate_name, v.voted_at
            FROM votes v JOIN candidates c ON c.id=v.candidate_id
            ORDER BY v.voted_at DESC
        """)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["user_id", "candidate_id", "candidate_name", "voted_at"])
        for r in rows:
            w.writerow([r["user_id"], r["candidate_id"], r["candidate_name"], r["voted_at"].isoformat()])
        data = out.getvalue().encode("utf-8")
        f = types.InputFile(io.BytesIO(data), filename="votes.csv")
        await c.message.answer_document(f, caption="📤 votes.csv")

    elif action == "results":
        text, kb = await results_text_and_buttons()
        await c.message.answer(text, reply_markup=kb)

    else:
        await c.message.answer("Номаълум амал")


# ----------------- ADMIN: BULK ADD NOMZOD (FSMsiz) -----------------
@dp.message_handler(lambda m: m.from_user and m.from_user.id in ADD_CANDIDATE_MODE)
async def add_candidates_auto(m: types.Message):
    if not is_admin(m.from_user.id):
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        return

    text = (m.text or "").strip()

    if text.lower() == "/cancel":
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        await m.answer("❌ Бекор қилинди.")
        return

    names = [x.strip() for x in text.split("\n") if x.strip()]
    if not names:
        await m.answer("⚠️ Номзод номларини юборинг (har qatorda bittadan).")
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
    await m.answer(
        f"✅ Қўшилди: {added}\n"
        f"⚠️ Такрор бўлгани учун ўтказиб юборилди: {skipped}",
        reply_markup=admin_kb()
    )

@dp.message_handler(commands=["cancel"])
async def cancel_any(m: types.Message):
    if m.from_user and m.from_user.id in ADD_CANDIDATE_MODE:
        ADD_CANDIDATE_MODE.discard(m.from_user.id)
        await m.answer("❌ Бекор қилинди.")
        return


# ----------------- ADMIN: ADD/REMOVE CHANNEL (FSM) -----------------
@dp.message_handler(state=AdminState.add_channel)
async def st_add_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    try:
        chat_id, join_url = normalize_channel_input(m.text)
    except Exception:
        await m.answer("❌ Канал формати нотўғри. Масалан: @kanal ёки https://t.me/kanal")
        await state.finish()
        return

    await db_execute("""
        INSERT INTO channels(chat_id, join_url)
        VALUES($1, $2)
        ON CONFLICT (chat_id) DO UPDATE SET join_url=EXCLUDED.join_url
    """, chat_id, join_url)

    await state.finish()
    await m.answer(f"✅ Канал қўшилди: <b>{chat_id}</b>", reply_markup=admin_kb())
    await m.answer("⚠️ Обуна текшируви ишлаши учун ботни каналга ADMIN қилинг.")

@dp.message_handler(state=AdminState.remove_channel)
async def st_rm_channel(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    raw = m.text.strip()
    # URL bo‘lsa normalize qilamiz
    try:
        chat_id, _url = normalize_channel_input(raw)
    except Exception:
        chat_id = raw.split()[0]

    await db_execute("DELETE FROM channels WHERE chat_id=$1", chat_id)
    await state.finish()
    await m.answer(f"✅ Канал ўчирилди (бор бўлса): <b>{chat_id}</b>", reply_markup=admin_kb())


# ----------------- ADMIN: REMOVE NOMZOD (ID yoki tartib raqam) -----------------
@dp.message_handler(state=AdminState.remove_candidate)
async def st_rm_candidate(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    raw = m.text.strip()

    # raqam bo‘lsa: avval ID deb urinadi, bo‘lmasa tartib raqami (1/2/3...)
    if raw.isdigit():
        n = int(raw)

        async with db_pool.acquire() as conn:
            res = await conn.execute("DELETE FROM candidates WHERE id=$1", n)
            deleted = int(res.split()[-1])
            if deleted == 1:
                await state.finish()
                await m.answer(f"✅ Номзод ўчирилди: ID <b>{n}</b>", reply_markup=admin_kb())
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
                await m.answer("❌ Бундай тартиб рақамдаги номзод топилмади.", reply_markup=admin_kb())
                return

            cid = int(row["id"])
            name = str(row["name"])
            await conn.execute("DELETE FROM candidates WHERE id=$1", cid)

        await state.finish()
        await m.answer(f"✅ Номзод ўчирилди: <b>{n}. {name}</b> (ID: {cid})", reply_markup=admin_kb())
        return

    # name bo‘yicha
    res = await db_execute("DELETE FROM candidates WHERE LOWER(name)=LOWER($1)", raw)
    deleted = int(res.split()[-1])

    await state.finish()
    if deleted:
        await m.answer(f"✅ Номзод ўчирилди: <b>{raw}</b>", reply_markup=admin_kb())
    else:
        await m.answer("❌ Номзод топилмади (номни текширинг).", reply_markup=admin_kb())


# ----------------- ADMIN: SET TIMER (FSM) -----------------
@dp.message_handler(state=AdminState.set_timer)
async def st_set_timer(m: types.Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        await state.finish()
        return

    raw = m.text.strip()
    if not raw.isdigit():
        await m.answer("Фақат рақам юборинг. Масалан: <code>60</code>")
        return

    minutes = int(raw)
    if minutes <= 0:
        await m.answer("0 дан катта бўлсин.")
        return

    end_time = now_utc() + timedelta(minutes=minutes)
    await set_setting("end_time_utc", end_time.isoformat())

    await state.finish()
    await m.answer(f"✅ Таймер ўрнатилди: <b>{minutes} дақиқа</b>\n{await remaining_time_text()}", reply_markup=admin_kb())


# ----------------- DB INIT -----------------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=10,
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
                name TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
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
