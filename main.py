import os, asyncio, uuid, datetime, random
from decimal import Decimal
import aiosqlite, aiohttp, qrcode
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from fastapi import FastAPI, Request, HTTPException
import uvicorn
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneNumberInvalidError, PhoneCodeExpiredError, FloodWaitError
)
from telethon.tl.types import Channel, Chat

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPORTE_USERNAME = os.getenv("SUPORTE_USERNAME", "LUCASLIMAMEI")
PIX_EMAIL = os.getenv("PIX_EMAIL", "doufzoficial@gmail.com")
SYNCPAY_CLIENT_ID = os.getenv("SYNCPAY_CLIENT_ID")
SYNCPAY_CLIENT_SECRET = os.getenv("SYNCPAY_CLIENT_SECRET")
SYNCPAY_BASE_URL = os.getenv("SYNCPAY_BASE_URL", "https://api.syncpayments.com.br")
TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
DB = "database.db"
MIN_INTERVAL_SECONDS = 30 * 60

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

LOGIN_STATE = {}
USER_TASKS = {}

PLANOS = {
    "p30m": {"nome": "30 minutos", "valor": Decimal("1.00"), "minutos": 30},
    "p1h": {"nome": "1 hora", "valor": Decimal("2.00"), "minutos": 60},
    "p2h": {"nome": "2 horas", "valor": Decimal("2.50"), "minutos": 120},
    "p3h": {"nome": "3 horas", "valor": Decimal("3.50"), "minutos": 180},
    "p6h": {"nome": "6 horas", "valor": Decimal("4.50"), "minutos": 360},
    "p12h": {"nome": "12 horas", "valor": Decimal("5.50"), "minutos": 720},
    "p1d": {"nome": "🔥 1 DIA", "valor": Decimal("7.00"), "minutos": 1440},
    "p2d": {"nome": "2 dias", "valor": Decimal("9.00"), "minutos": 2880},
    "p3d": {"nome": "3 dias", "valor": Decimal("10.00"), "minutos": 4320},
    "p5d": {"nome": "5 dias (2 links)", "valor": Decimal("13.00"), "minutos": 7200},
    "prio1d": {"nome": "🚀 Prioritária 1 DIA", "valor": Decimal("30.00"), "minutos": 1440},
}

# =========================
# BANCO DE DADOS
# =========================
async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            trial_used INTEGER DEFAULT 0, expires_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS orders(
            order_id TEXT PRIMARY KEY, user_id INTEGER, plano_id TEXT, amount TEXT,
            status TEXT, syncpay_id TEXT, pix_code TEXT, created_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS tg_sessions(
            user_id INTEGER PRIMARY KEY, phone TEXT, session_string TEXT, connected_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS temp_logins(
            user_id INTEGER PRIMARY KEY, phone TEXT, phone_code_hash TEXT,
            temp_session TEXT, created_at TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_config(
            user_id INTEGER PRIMARY KEY, message TEXT, interval_seconds INTEGER DEFAULT 3600,
            running INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_chats(
            user_id INTEGER, chat_id INTEGER, title TEXT,
            PRIMARY KEY(user_id, chat_id)
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS referrals(
            code TEXT PRIMARY KEY, user_id INTEGER, used_by INTEGER, used_at TEXT
        )""")
        await db.commit()

async def ensure_user(m: Message):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id, username, first_name) VALUES(?,?,?)", (m.from_user.id, m.from_user.username, m.from_user.first_name))
        await db.execute("INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)", (m.from_user.id, 3600, 0))
        await db.commit()

# =========================
# REFERRAL SYSTEM
# =========================
async def generate_referral_code(user_id: int) -> str:
    code = f"loop{random.randint(10000, 99999)}"
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO referrals(code, user_id) VALUES(?,?)", (code, user_id))
        await db.commit()
    return code

async def redeem_referral_code(m: Message, code: str):
    user_id = m.from_user.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, used_by FROM referrals WHERE code=?", (code,))
        row = await cur.fetchone()
        if not row:
            await m.answer("❌ Código inválido.")
            return
        owner_id, used_by = row
        if used_by is not None:
            await m.answer("❌ Esse código já foi usado.")
            return
        if owner_id == user_id:
            await m.answer("❌ Você não pode usar seu próprio código.")
            return

        await db.execute("UPDATE referrals SET used_by=?, used_at=? WHERE code=?", (user_id, datetime.datetime.utcnow().isoformat(), code))
        await db.commit()

    await add_time(owner_id, 24 * 60)   # 1 dia para quem indicou
    await add_time(user_id, 12 * 60)    # 12 horas para o novo

    await m.answer("🎉 **Código resgatado com sucesso!**\n\n✅ Você ganhou **12 horas**\n✅ Quem te indicou ganhou **1 dia** extra")

async def add_time(user_id: int, minutes: int):
    now = datetime.datetime.utcnow()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        base = now
        if row and row[0]:
            try:
                old = datetime.datetime.fromisoformat(row[0])
                if old > now: base = old
            except: pass
        new_exp = base + datetime.timedelta(minutes=minutes)
        await db.execute("UPDATE users SET expires_at=? WHERE user_id=?", (new_exp.isoformat(), user_id))
        await db.commit()
        return new_exp

async def is_active(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    if not row or not row[0]: return False
    try:
        return datetime.datetime.fromisoformat(row[0]) > datetime.datetime.utcnow()
    except: return False

# =========================
# MENUS
# =========================
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 RESGATAR 3 DIAS GRÁTIS", callback_data="trial")],
        [InlineKeyboardButton(text="🔗 MEU LINK DE INDICAÇÃO", callback_data="my_referral")],
        [InlineKeyboardButton(text="💎 VER PLANOS", callback_data="planos")],
        [InlineKeyboardButton(text="⚙️ CONFIGURAR LOOP", callback_data="config_loop")],
        [InlineKeyboardButton(text="👤 MEU PERFIL", callback_data="perfil")],
        [InlineKeyboardButton(text="📖 COMO CONFIGURAR", url="https://t.me/aulasloopgram")],
        [InlineKeyboardButton(text="📞 SUPORTE", url=f"https://t.me/{SUPORTE_USERNAME}")],
    ])

# ... Continue na PARTE 2
def config_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 CONECTAR CONTA TELEGRAM", callback_data="connect_account")],
        [InlineKeyboardButton(text="📢 MEUS GRUPOS/CANAIS", callback_data="my_chats")],
        [InlineKeyboardButton(text="📝 DEFINIR MENSAGEM", callback_data="set_message")],
        [InlineKeyboardButton(text="⏱️ DEFINIR INTERVALO", callback_data="set_interval")],
        [InlineKeyboardButton(text="▶️ INICIAR LOOP", callback_data="start_loop")],
        [InlineKeyboardButton(text="⏹️ PARAR LOOP", callback_data="stop_loop")],
        [InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="voltar")],
    ])

def planos_kb():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANOS.items():
        kb.button(text=f"{p['nome']} — R${p['valor']}", callback_data=f"comprar:{pid}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="voltar"))
    return kb.as_markup()

# =========================
# TELETHON
# =========================
def make_client(session_string: str = ""):
    return TelegramClient(StringSession(session_string), TELEGRAM_API_ID, TELEGRAM_API_HASH)

async def get_session(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT session_string FROM tg_sessions WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    return row[0] if row else None

async def save_session(user_id: int, phone: str, session_string: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR REPLACE INTO tg_sessions(user_id, phone, session_string, connected_at) VALUES(?,?,?,?)",
                         (user_id, phone, session_string, datetime.datetime.utcnow().isoformat()))
        await db.commit()

# (Mantenha todas as funções telethon que você já tinha: save_temp_login, get_temp_login, get_admin_chats, etc.)

# =========================
# HANDLERS
# =========================
@dp.message(Command("start"))
async def start(m: Message):
    await ensure_user(m)
    args = m.text.split()[1:] if len(m.text.split()) > 1 else []
    if args:
        code = args[0].strip()
        if code.startswith("loop"):
            await redeem_referral_code(m, code)
            return

    await m.answer(
        "👋 Olá! Seja bem-vindo(a) ao Loop Mensage!\n\n"
        "Automação inteligente para divulgação no Telegram.",
        reply_markup=main_kb()
    )

@dp.callback_query(F.data == "my_referral")
async def my_referral(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT code FROM referrals WHERE user_id=? AND used_by IS NULL", (user_id,))
        row = await cur.fetchone()
        code = row[0] if row else await generate_referral_code(user_id)

    bot_user = await bot.get_me()
    link = f"https://t.me/{bot_user.username}?start={code}"

    await c.message.answer(
        f"🔗 **Seu Link de Indicação**\n\n"
        f"{link}\n\n"
        f"**Código:** `{code}`\n\n"
        "Envie para seus amigos!", parse_mode="Markdown"
    )

# Cole aqui todo o resto do seu código original (do @dp.callback_query(F.data == "voltar") até o final)

@dp.callback_query(F.data == "voltar")
async def voltar(c: CallbackQuery):
    await c.message.edit_text("Menu principal:", reply_markup=main_kb())

# ... (coloque todo o resto aqui: perfil, trial, planos, config_loop, connect_account, etc.)

# =========================
# RUN
# =========================
async def run_bot():
    await db_init()
    await dp.start_polling(bot)

async def run_api():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
