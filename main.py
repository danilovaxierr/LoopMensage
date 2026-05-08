import os, asyncio, json, uuid, datetime
from decimal import Decimal
from typing import Optional

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
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError, PhoneNumberInvalidError
from telethon.tl.types import Channel, Chat

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPORTE_USERNAME = os.getenv("SUPORTE_USERNAME", "LUCASLIMAMEI")
PIX_EMAIL = os.getenv("PIX_EMAIL", "doufzoficial@gmail.com")
SYNCPAY_CLIENT_ID = os.getenv("SYNCPAY_CLIENT_ID")
SYNCPAY_CLIENT_SECRET = os.getenv("SYNCPAY_CLIENT_SECRET")
SYNCPAY_BASE_URL = os.getenv("SYNCPAY_BASE_URL", "https://api.syncpayments.com.br")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "troque-isso")

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")

DB = "database.db"
MIN_INTERVAL_SECONDS = 30 * 60  # mínimo seguro: 30 minutos

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

# Estados simples em memória. Em produção maior, use FSM/Redis.
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
# BANCO
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
            user_id INTEGER PRIMARY KEY,
            phone TEXT,
            session_string TEXT,
            connected_at TEXT
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS loop_config(
            user_id INTEGER PRIMARY KEY,
            message TEXT,
            interval_seconds INTEGER DEFAULT 3600,
            running INTEGER DEFAULT 0
        )""")

        await db.execute("""CREATE TABLE IF NOT EXISTS loop_chats(
            user_id INTEGER,
            chat_id INTEGER,
            title TEXT,
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

async def profile_text(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT trial_used, expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        cur2 = await db.execute("SELECT COUNT(*) FROM loop_chats WHERE user_id=?", (user_id,))
        chats_count = (await cur2.fetchone())[0]
        cur3 = await db.execute("SELECT interval_seconds, running FROM loop_config WHERE user_id=?", (user_id,))
        cfg = await cur3.fetchone()

    exp = row[1] if row else None
    interval_txt = f"{int((cfg[0] if cfg else 3600) / 60)} min"
    running_txt = "ativo" if cfg and cfg[1] == 1 else "parado"

    extra = f"\n\n📢 Grupos/canais selecionados: {chats_count}\n⏱️ Intervalo: {interval_txt}\n🔁 Loop: {running_txt}"

    if not exp:
        return "👤 Meu perfil\n\n⏳ Assinatura: sem tempo ativo" + extra
    exp_dt = datetime.datetime.fromisoformat(exp)
    left = exp_dt - datetime.datetime.utcnow()
    if left.total_seconds() <= 0:
        return "👤 Meu perfil\n\n⚠️ Assinatura expirada" + extra
    return f"👤 Meu perfil\n\n✅ Ativo até: {exp_dt.strftime('%d/%m/%Y %H:%M')} UTC\n⏳ Tempo restante: {str(left).split('.')[0]}" + extra

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

def planos_kb():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANOS.items():
        kb.button(text=f"{p['nome']} — R${p['valor']}", callback_data=f"comprar:{pid}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="voltar"))
    return kb.as_markup()

def chats_kb(chats):
    kb = InlineKeyboardBuilder()
    for chat_id, title, selected in chats:
        mark = "✅" if selected else "⬜"
        safe_title = title[:45]
        kb.button(text=f"{mark} {safe_title}", callback_data=f"toggle_chat:{chat_id}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔄 ATUALIZAR LISTA", callback_data="my_chats"))
    kb.row(InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="config_loop"))
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
        await db.execute(
            "INSERT OR REPLACE INTO tg_sessions(user_id, phone, session_string, connected_at) VALUES(?,?,?,?)",
            (user_id, phone, session_string, datetime.datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_admin_chats(user_id: int):
    session = await get_session(user_id)
    if not session:
        return []

    result = []
    client = make_client(session)
    async with client:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            title = getattr(entity, "title", None)
            if not title:
                continue

            # Somente grupos/canais onde a própria conta tem creator/admin_rights.
            is_admin = False
            if isinstance(entity, Channel):
                if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
                    is_admin = True
            elif isinstance(entity, Chat):
                if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
                    is_admin = True

            if is_admin:
                result.append((int(dialog.id), title))

    return result

async def get_selected_chats(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT chat_id, title FROM loop_chats WHERE user_id=?", (user_id,))
        return await cur.fetchall()

# =========================
# HANDLERS PRINCIPAIS
# =========================

@dp.message(Command("start"))
async def start(m: Message):
    await ensure_user(m)
    await m.answer(
        "👋 Olá! Seja bem-vindo(a) ao Loop Mensage!\n\n"
        "Automação inteligente para divulgação no Telegram.\n\n"
        "🎁 Você pode testar por 3 dias grátis.",
        reply_markup=main_kb()
    )

@dp.callback_query(F.data == "voltar")
async def voltar(c: CallbackQuery):
    await c.message.edit_text("Menu principal:", reply_markup=main_kb())

@dp.callback_query(F.data == "perfil")
async def perfil(c: CallbackQuery):
    await c.answer()
    await c.message.answer(await profile_text(c.from_user.id))

@dp.callback_query(F.data == "trial")
async def trial(c: CallbackQuery):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_name) VALUES(?,?,?)",
            (c.from_user.id, c.from_user.username, c.from_user.first_name)
        )
        await db.execute(
            "INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)",
            (c.from_user.id, 3600, 0)
        )
        cur = await db.execute("SELECT trial_used FROM users WHERE user_id=?", (c.from_user.id,))
        row = await cur.fetchone()
        if row and row[0] == 1:
            await c.answer("Você já usou o teste grátis.", show_alert=True)
            return
        await db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (c.from_user.id,))
        await db.commit()

    exp = await add_time(c.from_user.id, 3 * 24 * 60)
    await c.message.answer(f"🎁 3 dias grátis ativados!\n✅ Ativo até {exp.strftime('%d/%m/%Y %H:%M')} UTC")

@dp.callback_query(F.data == "planos")
async def planos(c: CallbackQuery):
    texto = "💎 PLANOS DISPONÍVEIS\n\nEscolha um plano abaixo para gerar Pix automático com QR Code."
    await c.message.edit_text(texto, reply_markup=planos_kb())

@dp.callback_query(F.data == "config_loop")
async def config_loop(c: CallbackQuery):
    texto = (
        "⚙️ CONFIGURAR LOOP MENSAGE\n\n"
        "Aqui você configura sua divulgação automática segura.\n\n"
        "✅ Permitido:\n"
        "• Grupos onde você é admin\n"
        "• Canais onde você é admin\n"
        "• Mensagens com intervalo alto\n"
        "• Pausar quando quiser\n\n"
        "🚫 Não permitido:\n"
        "• Enviar em grupos onde você não é admin\n"
        "• Enviar privado para pessoas\n"
        "• Spam em massa\n\n"
        "Escolha uma opção:"
    )
    await c.message.edit_text(texto, reply_markup=config_kb())

@dp.callback_query(F.data == "connect_account")
async def connect_account(c: CallbackQuery):
    await c.answer()

    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        await c.message.answer("❌ TELEGRAM_API_ID ou TELEGRAM_API_HASH não configurado no Render.")
        return

    LOGIN_STATE[c.from_user.id] = {"step": "phone"}
    await c.message.answer(
        "🔐 CONECTAR CONTA TELEGRAM\n\n"
        "Envie seu número no formato internacional.\n\n"
        "Exemplo:\n"
        "+5521999999999\n\n"
        "⚠️ Use apenas para divulgar em grupos/canais onde você é admin."
    )

@dp.callback_query(F.data == "my_chats")
async def my_chats(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id

    session = await get_session(user_id)
    if not session:
        await c.message.answer("❌ Conecte sua conta Telegram primeiro.")
        return

    await c.message.answer("🔎 Buscando grupos/canais onde você é admin...")
    try:
        admin_chats = await get_admin_chats(user_id)
    except Exception as e:
        await c.message.answer(f"❌ Erro ao buscar grupos/canais: {e}")
        return

    selected = {str(row[0]) for row in await get_selected_chats(user_id)}
    chats = [(chat_id, title, str(chat_id) in selected) for chat_id, title in admin_chats]

    if not chats:
        await c.message.answer("Nenhum grupo/canal admin encontrado nessa conta.")
        return

    await c.message.answer(
        "📢 Selecione onde o loop pode postar.\n\n"
        "Só aparecem grupos/canais onde essa conta é admin:",
        reply_markup=chats_kb(chats)
    )

@dp.callback_query(F.data.startswith("toggle_chat:"))
async def toggle_chat(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    chat_id = int(c.data.split(":", 1)[1])

    admin_chats = await get_admin_chats(user_id)
    titles = {int(cid): title for cid, title in admin_chats}

    if chat_id not in titles:
        await c.message.answer("❌ Esse chat não está permitido. Você precisa ser admin.")
        return

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT chat_id FROM loop_chats WHERE user_id=? AND chat_id=?", (user_id, chat_id))
        exists = await cur.fetchone()

        if exists:
            await db.execute("DELETE FROM loop_chats WHERE user_id=? AND chat_id=?", (user_id, chat_id))
            await db.commit()
            await c.message.answer(f"⬜ Removido: {titles[chat_id]}")
        else:
            await db.execute(
                "INSERT OR REPLACE INTO loop_chats(user_id, chat_id, title) VALUES(?,?,?)",
                (user_id, chat_id, titles[chat_id])
            )
            await db.commit()
            await c.message.answer(f"✅ Selecionado: {titles[chat_id]}")

@dp.callback_query(F.data == "set_message")
async def set_message(c: CallbackQuery):
    await c.answer()
    LOGIN_STATE[c.from_user.id] = {"step": "set_message"}
    await c.message.answer(
        "📝 Envie agora a mensagem que será postada.\n\n"
        "⚠️ Evite conteúdo enganoso, spam ou promessas falsas."
    )

@dp.callback_query(F.data == "set_interval")
async def set_interval(c: CallbackQuery):
    await c.answer()
    LOGIN_STATE[c.from_user.id] = {"step": "set_interval"}
    await c.message.answer(
        "⏱️ Envie o intervalo em minutos.\n\n"
        "Mínimo permitido: 30 minutos.\n"
        "Exemplo: 60"
    )

@dp.callback_query(F.data == "start_loop")
async def start_loop(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id

    if not await is_active(user_id):
        await c.message.answer("⚠️ Sua assinatura está sem tempo ativo. Ative o teste grátis ou compre um plano.")
        return

    session = await get_session(user_id)
    if not session:
        await c.message.answer("❌ Conecte sua conta Telegram primeiro.")
        return

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT message, interval_seconds FROM loop_config WHERE user_id=?", (user_id,))
        cfg = await cur.fetchone()
        cur2 = await db.execute("SELECT chat_id FROM loop_chats WHERE user_id=?", (user_id,))
        chats = await cur2.fetchall()

    if not cfg or not cfg[0]:
        await c.message.answer("❌ Defina uma mensagem primeiro.")
        return

    if not chats:
        await c.message.answer("❌ Selecione pelo menos 1 grupo/canal onde você é admin.")
        return

    await set_running(user_id, 1)
    await start_user_loop(user_id)
    await c.message.answer("▶️ Loop iniciado com segurança.\nEle só enviará nos grupos/canais selecionados onde você é admin.")

@dp.callback_query(F.data == "stop_loop")
async def stop_loop(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    await set_running(user_id, 0)

    task = USER_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()

    await c.message.answer("⏹️ Loop parado.")

# =========================
# MENSAGENS DE ESTADO
# =========================

@dp.message()
async def state_messages(m: Message):
    user_id = m.from_user.id
    state = LOGIN_STATE.get(user_id)

    if not state:
        return

    step = state.get("step")

    if step == "phone":
        phone = m.text.strip()
        try:
            client = make_client()
            await client.connect()
            sent = await client.send_code_request(phone)
            LOGIN_STATE[user_id] = {
                "step": "code",
                "phone": phone,
                "phone_code_hash": sent.phone_code_hash,
                "client": client
            }
            await m.answer(
                "✅ Código enviado para seu Telegram.\n\n"
                "Envie o código aqui.\n"
                "Exemplo: 12345"
            )
        except PhoneNumberInvalidError:
            await m.answer("❌ Número inválido. Use formato internacional, exemplo: +5521999999999")
        except Exception as e:
            await m.answer(f"❌ Erro ao enviar código: {e}")
        return

    if step == "code":
        code = m.text.strip().replace(" ", "")
        phone = state["phone"]
        phone_code_hash = state["phone_code_hash"]
        client = state["client"]

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            session_string = client.session.save()
            await save_session(user_id, phone, session_string)
            await client.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await m.answer(
                "✅ Conta conectada com sucesso!\n\n"
                "Agora vá em:\n"
                "📢 MEUS GRUPOS/CANAIS\n"
                "e selecione onde você é admin."
            )
        except SessionPasswordNeededError:
            LOGIN_STATE[user_id]["step"] = "password"
            await m.answer("🔐 Sua conta tem senha 2FA. Envie sua senha para finalizar o login.")
        except PhoneCodeInvalidError:
            await m.answer("❌ Código inválido. Tente enviar novamente.")
        except Exception as e:
            await m.answer(f"❌ Erro no login: {e}")
        return

    if step == "password":
        password = m.text.strip()
        client = state["client"]
        phone = state["phone"]

        try:
            await client.sign_in(password=password)
            session_string = client.session.save()
            await save_session(user_id, phone, session_string)
            await client.disconnect()
            LOGIN_STATE.pop(user_id, None)
            await m.answer(
                "✅ Conta conectada com sucesso!\n\n"
                "Agora vá em:\n"
                "📢 MEUS GRUPOS/CANAIS\n"
                "e selecione onde você é admin."
            )
        except Exception as e:
            await m.answer(f"❌ Senha 2FA inválida ou erro no login: {e}")
        return

    if step == "set_message":
        text = m.text.strip()
        if len(text) < 3:
            await m.answer("❌ Mensagem muito curta.")
            return

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)",
                (user_id, 3600, 0)
            )
            await db.execute("UPDATE loop_config SET message=? WHERE user_id=?", (text, user_id))
            await db.commit()

        LOGIN_STATE.pop(user_id, None)
        await m.answer("✅ Mensagem salva.")
        return

    if step == "set_interval":
        try:
            minutes = int(m.text.strip())
        except Exception:
            await m.answer("❌ Envie só o número em minutos. Exemplo: 60")
            return

        seconds = minutes * 60
        if seconds < MIN_INTERVAL_SECONDS:
            await m.answer("❌ Intervalo mínimo é 30 minutos.")
            return

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)",
                (user_id, 3600, 0)
            )
            await db.execute("UPDATE loop_config SET interval_seconds=? WHERE user_id=?", (seconds, user_id))
            await db.commit()

        LOGIN_STATE.pop(user_id, None)
        await m.answer(f"✅ Intervalo salvo: {minutes} minutos.")
        return

# =========================
# LOOP SEGURO
# =========================

async def set_running(user_id: int, running: int):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR IGNORE INTO loop_config(user_id, interval_seconds, running) VALUES(?,?,?)",
            (user_id, 3600, 0)
        )
        await db.execute("UPDATE loop_config SET running=? WHERE user_id=?", (running, user_id))
        await db.commit()

async def loop_worker(user_id: int):
    while True:
        try:
            if not await is_active(user_id):
                await set_running(user_id, 0)
                await bot.send_message(user_id, "⚠️ Sua assinatura expirou. Loop pausado automaticamente.")
                return

            async with aiosqlite.connect(DB) as db:
                cur = await db.execute("SELECT message, interval_seconds, running FROM loop_config WHERE user_id=?", (user_id,))
                cfg = await cur.fetchone()
                cur2 = await db.execute("SELECT chat_id, title FROM loop_chats WHERE user_id=?", (user_id,))
                chats = await cur2.fetchall()

            if not cfg or cfg[2] != 1:
                return

            message, interval_seconds, _ = cfg
            interval_seconds = max(int(interval_seconds or 3600), MIN_INTERVAL_SECONDS)

            session = await get_session(user_id)
            if not session:
                await set_running(user_id, 0)
                await bot.send_message(user_id, "❌ Sessão Telegram não encontrada. Loop pausado.")
                return

            client = make_client(session)
            sent_count = 0

            async with client:
                # Revalida admin a cada ciclo.
                admin_chats = await get_admin_chats(user_id)
                allowed = {int(cid) for cid, _title in admin_chats}

                for chat_id, title in chats:
                    if int(chat_id) not in allowed:
                        continue
                    try:
                        await client.send_message(int(chat_id), message)
                        sent_count += 1
                        await asyncio.sleep(5)
                    except Exception:
                        pass

            try:
                await bot.send_message(user_id, f"✅ Ciclo concluído. Mensagem enviada em {sent_count} grupos/canais permitidos.")
            except Exception:
                pass

            await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            return
        except Exception as e:
            try:
                await bot.send_message(user_id, f"⚠️ Erro no loop: {e}\nTentando novamente no próximo ciclo.")
            except Exception:
                pass
            await asyncio.sleep(60)

async def start_user_loop(user_id: int):
    old = USER_TASKS.get(user_id)
    if old and not old.done():
        old.cancel()
    USER_TASKS[user_id] = asyncio.create_task(loop_worker(user_id))

async def restore_running_loops():
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM loop_config WHERE running=1")
        rows = await cur.fetchall()

    for (user_id,) in rows:
        if await is_active(user_id):
            await start_user_loop(user_id)

# =========================
# SYNCPAY
# =========================

async def syncpay_create_pix(order_id: str, amount: Decimal, user_id: int, desc: str):
    async with aiohttp.ClientSession() as s:
        token_resp = await s.post(f"{SYNCPAY_BASE_URL}/api/partner/v1/auth-token", json={
            "client_id": SYNCPAY_CLIENT_ID,
            "client_secret": SYNCPAY_CLIENT_SECRET,
        })
        token_data = await token_resp.json(content_type=None)
        token = token_data.get("access_token") or token_data.get("token")
        if not token:
            raise RuntimeError(f"Erro auth SyncPay: {token_data}")

        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "amount": float(amount),
            "description": desc,
            "external_id": order_id,
            "webhook_url": os.getenv("PUBLIC_WEBHOOK_URL"),
            "customer": {"name": f"Telegram {user_id}", "email": PIX_EMAIL}
        }

        r = await s.post(f"{SYNCPAY_BASE_URL}/api/partner/v1/cash-in", headers=headers, json=payload)
        data = await r.json(content_type=None)

        pix_code = data.get("pix_code") or data.get("copy_paste") or data.get("qrcode") or data.get("qr_code")
        sync_id = data.get("id") or data.get("transaction_id")

        if not pix_code:
            raise RuntimeError(f"Erro criando Pix SyncPay: {data}")

        return sync_id, pix_code

@dp.callback_query(F.data.startswith("comprar:"))
async def comprar(c: CallbackQuery):
    pid = c.data.split(":", 1)[1]
    p = PLANOS[pid]
    order_id = str(uuid.uuid4())

    await c.message.answer("⏳ Gerando Pix automático...")

    try:
        sync_id, pix_code = await syncpay_create_pix(order_id, p["valor"], c.from_user.id, f"Loop Mensage - {p['nome']}")
    except Exception as e:
        await c.message.answer(f"❌ Erro ao gerar Pix. Verifique as credenciais/endpoints da SyncPay.\n\nDetalhe: {e}")
        return

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
            (order_id, c.from_user.id, pid, str(p["valor"]), "pending", sync_id, pix_code, datetime.datetime.utcnow().isoformat())
        )
        await db.commit()

    img = qrcode.make(pix_code)
    import io
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)

    await c.message.answer_photo(
        BufferedInputFile(bio.read(), filename="pix.png"),
        caption=f"✅ Pix gerado para {p['nome']} — R${p['valor']}\n\nCopia e cola:\n<code>{pix_code}</code>",
        parse_mode="HTML"
    )

@app.post("/syncpay/webhook")
async def syncpay_webhook(request: Request):
    data = await request.json()

    status = str(data.get("status") or data.get("payment_status") or "").lower()
    order_id = data.get("external_id") or data.get("reference") or data.get("metadata", {}).get("order_id")

    if not order_id:
        raise HTTPException(400, "external_id ausente")

    if status not in ["paid", "approved", "confirmed", "completed"]:
        return {"ok": True, "ignored": status}

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id, plano_id, status FROM orders WHERE order_id=?", (order_id,))
        row = await cur.fetchone()

        if not row:
            raise HTTPException(404, "pedido não encontrado")

        user_id, plano_id, old_status = row

        if old_status == "paid":
            return {"ok": True, "already_paid": True}

        await db.execute("UPDATE orders SET status='paid' WHERE order_id=?", (order_id,))
        await db.commit()

    exp = await add_time(user_id, PLANOS[plano_id]["minutos"])
    await bot.send_message(
        user_id,
        f"✅ Pagamento confirmado!\nSeu plano {PLANOS[plano_id]['nome']} foi ativado.\nAtivo até {exp.strftime('%d/%m/%Y %H:%M')} UTC"
    )
    return {"ok": True}

# =========================
# ADMIN
# =========================

@dp.message(Command("adddias"))
async def adddias(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    parts = m.text.split()
    if len(parts) != 3:
        await m.answer("Use: /adddias ID dias")
        return

    user_id, dias = int(parts[1]), int(parts[2])
    exp = await add_time(user_id, dias * 24 * 60)
    await m.answer(f"✅ Adicionado {dias} dias para {user_id}. Expira em {exp}")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if m.from_user.id != ADMIN_ID:
        return

    texto = m.text.replace('/broadcast', '', 1).strip()
    if not texto:
        await m.answer("Use: /broadcast mensagem")
        return

    enviados = 0
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users")
        async for (uid,) in cur:
            try:
                await bot.send_message(uid, texto)
                enviados += 1
            except Exception:
                pass

    await m.answer(f"✅ Enviado para {enviados} usuários.")

# =========================
# RUN
# =========================

async def run_bot():
    await db_init()
    await restore_running_loops()
    await dp.start_polling(bot)

async def run_api():
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

async def main():
    await asyncio.gather(run_bot(), run_api())

if __name__ == "__main__":
    asyncio.run(main())
