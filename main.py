import os
import asyncio
import asyncpg
from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

db_pool: asyncpg.Pool | None = None


# ---------- DATABASE ----------
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=30
    )

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            user_id BIGINT PRIMARY KEY,
            choice TEXT NOT NULL,
            voted_at TIMESTAMP DEFAULT NOW()
        )
        """)


# ---------- HANDLERS ----------
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("1️⃣ Variant A", callback_data="vote_A"),
        types.InlineKeyboardButton("2️⃣ Variant B", callback_data="vote_B"),
    )
    await message.answer("Ovoz bering:", reply_markup=kb)


@dp.callback_query_handler(lambda c: c.data.startswith("vote_"))
async def vote_handler(call: types.CallbackQuery):
    choice = call.data.replace("vote_", "")

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO votes (user_id, choice)
            VALUES ($1, $2)
            ON CONFLICT (user_id)
            DO UPDATE SET choice = EXCLUDED.choice, voted_at = NOW()
        """, call.from_user.id, choice)

    await call.answer("✅ Ovozingiz qabul qilindi", show_alert=True)


# ---------- STARTUP / SHUTDOWN ----------
async def on_startup(dp):
    await init_db()
    print("DB: POSTGRES | READY")
    print("BOT STARTED")


async def on_shutdown(dp):
    if db_pool:
        await db_pool.close()


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
