import os, asyncio, json, uuid, datetime
from decimal import Decimal

import aiosqlite, aiohttp, qrcode
import re

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
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError
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
            user_id INTEGER PRIMARY KEY, message TEXT,
            interval_seconds INTEGER DEFAULT 3600, running INTEGER DEFAULT 0
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS loop_chats(
            user_id INTEGER, chat_id INTEGER, title TEXT,
            PRIMARY KEY(user_id, chat_id)
        )""")
        await db.commit()


async def ensure_user(m: Message):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name) VALUES(?,?,?)",
            (m.from_user.id, m.from_user.username, m.from_user.first_name)
        )
        await db.execute(
            "INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)",
            (m.from_user.id, 3600, 0)
        )
        await db.commit()


async def add_time(user_id: int, minutes: int):
    now = datetime.datetime.utcnow()
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        base = now
        if row and row[0]:
            try:
                old = datetime.datetime.fromisoformat(row[0])
                if old > now:
                    base = old
            except Exception:
                pass
        new_exp = base + datetime.timedelta(minutes=minutes)
        await db.execute("UPDATE users SET expires_at=? WHERE user_id=?", (new_exp.isoformat(), user_id))
        await db.commit()
        return new_exp


async def is_active(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    if not row or not row[0]:
        return False
    try:
        return datetime.datetime.fromisoformat(row[0]) > datetime.datetime.utcnow()
    except Exception:
        return False


# =========================
# MENUS
# =========================

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 RESGATAR 3 DIAS GRÁTIS", callback_data="trial")],
        [InlineKeyboardButton(text="💎 VER PLANOS", callback_data="planos")],
        [InlineKeyboardButton(text="⚙️ CONFIGURAR LOOP", callback_data="config_loop")],
        [InlineKeyboardButton(text="👤 MEU PERFIL", callback_data="perfil")],
        [InlineKeyboardButton(text="📖 COMO CONFIGURAR", url="https://t.me/aulasloopgram")],
        [InlineKeyboardButton(text="📞 SUPORTE", url=f"https://t.me/{SUPORTE_USERNAME}")],
    ])


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
        await db.execute(
            "INSERT OR REPLACE INTO tg_sessions(user_id, phone, session_string, connected_at) VALUES(?,?,?,?)",
            (user_id, phone, session_string, datetime.datetime.utcnow().isoformat())
        )
        await db.commit()


async def save_temp_login(user_id: int, phone: str, phone_code_hash: str, temp_session: str):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO temp_logins(user_id, phone, phone_code_hash, temp_session, created_at) VALUES(?,?,?,?,?)",
            (user_id, phone, phone_code_hash, temp_session, datetime.datetime.utcnow().isoformat())
        )
        await db.commit()


async def get_temp_login(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT phone, phone_code_hash, temp_session, created_at FROM temp_logins WHERE user_id=?",
            (user_id,)
        )
        return await cur.fetchone()


async def clear_temp_login(user_id: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM temp_logins WHERE user_id=?", (user_id,))
        await db.commit()


# =========================
# HANDLERS PRINCIPAIS
# =========================

@dp.message(Command("start"))
async def start(m: Message):
    await ensure_user(m)
    await m.answer(
        "👋 Olá! Seja bem-vindo ao Loop Mensage!",
        reply_markup=main_kb()
    )


@dp.callback_query(F.data == "connect_account")
async def connect_account(c: CallbackQuery):
    await c.answer()
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await c.message.answer("❌ TELEGRAM_API_ID ou TELEGRAM_API_HASH não configurado.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 QR Code (Recomendado)", callback_data="login_qr")],
        [InlineKeyboardButton(text="📞 Número + Código", callback_data="login_phone")]
    ])
    await c.message.answer("🔐 Escolha o método de login:", reply_markup=kb)


@dp.callback_query(F.data == "login_qr")
async def login_qr_callback(c: CallbackQuery):
    await c.answer()
    await c.message.answer("🔐 Gerando QR Code...")
    await start_qr_login(c.from_user.id, c.message)


@dp.callback_query(F.data == "login_phone")
async def login_phone_callback(c: CallbackQuery):
    await c.answer()
    LOGIN_STATE[c.from_user.id] = {"step": "phone"}
    await c.message.answer(
        "🔐 Envie seu número no formato internacional:\n\nExemplo: `+5521999999999`",
        parse_mode="Markdown"
    )
    # =========================
# MENSAGENS DE ESTADO (COM loop12345)
# =========================

@dp.message()
async def state_messages(m: Message):
    user_id = m.from_user.id
    state = LOGIN_STATE.get(user_id)
    if not state:
        return

    step = state.get("step")

    if step == "phone":
        phone = m.text.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        try:
            client = make_client()
            await client.connect()
            sent = await client.send_code_request(phone)

            await save_temp_login(
                user_id=user_id,
                phone=phone,
                phone_code_hash=sent.phone_code_hash,
                temp_session=client.session.save()
            )

            LOGIN_STATE[user_id] = {
                "step": "code",
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "client": client
            }

            await m.answer(
                "✅ Código enviado!\n\n"
                "Envie no formato:\n"
                "`loop12345`\n\n"
                "Exemplo: `loop54213`",
                parse_mode="Markdown"
            )
        except Exception as e:
            await m.answer(f"❌ Erro: {e}")
        return

    if step == "code":
        text = m.text.strip()
        code = re.sub(r'(?i)^loop', '', text).strip()
        code = ''.join(filter(str.isdigit, code))

        if len(code) < 4:
            await m.answer("❌ Código inválido.\nEnvie: `loop12345`")
            return

        phone = state.get("phone")
        phone_code_hash = state.get("phone_code_hash")
        client = state.get("client")

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            session_string = client.session.save()
            await save_session(user_id, phone, session_string)
            await clear_temp_login(user_id)
            LOGIN_STATE.pop(user_id, None)
            await m.answer("✅ Conta conectada com sucesso!\n\nVá em 📢 MEUS GRUPOS/CANAIS")
        except Exception as e:
            await m.answer(f"❌ Erro: {str(e)}")
        return


# =========================
# CALLBACKS FALTANTES (IMPORTANTE)
# =========================

@dp.callback_query(F.data == "planos")
async def planos(c: CallbackQuery):
    await c.message.edit_text("💎 Escolha um plano:", reply_markup=planos_kb())

@dp.callback_query(F.data == "config_loop")
async def config_loop(c: CallbackQuery):
    await c.message.edit_text(
        "⚙️ Configurar Loop\n\nEscolha uma opção:",
        reply_markup=config_kb()
    )

@dp.callback_query(F.data == "trial")
async def trial(c: CallbackQuery):
    await c.answer("Em breve...", show_alert=True)

# =========================
# RUN (PARA RENDER)
# =========================

async def run_bot():
    await db_init()
    print("🤖 Bot iniciado com sucesso!")
    await dp.start_polling(bot)


async def run_api():
    config = uvicorn.Config(
        app, 
        host="0.0.0.0", 
        port=int(os.getenv("PORT", 8000)),
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    await asyncio.gather(run_bot(), run_api())


if __name__ == "__main__":
    asyncio.run(main())
