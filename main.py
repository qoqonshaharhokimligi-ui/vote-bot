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

ADMINS = [32257986]  # admin user_id

UTC = timezone.utc

bot = Bot(API_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ----------------- DB -----------------
db = sqlite3.connect("db.sqlite", check_same_thread=False)
sql = db.cursor()

sql.execute("""
CREATE TABLE IF NOT EXISTS channels(
    chat_id TEXT PRIMARY KEY,
    join_url TEXT
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
    return datetime.fromisoformat(row[0])

def voting_is_open() -> bool:
    end_time = get_end_time()
    return not end_tim_
