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

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUPORTE_USERNAME = os.getenv("SUPORTE_USERNAME", "LUCASLIMAMEI")
PIX_EMAIL = os.getenv("PIX_EMAIL", "doufzoficial@gmail.com")
SYNCPAY_CLIENT_ID = os.getenv("SYNCPAY_CLIENT_ID")
SYNCPAY_CLIENT_SECRET = os.getenv("SYNCPAY_CLIENT_SECRET")
SYNCPAY_BASE_URL = os.getenv("SYNCPAY_BASE_URL", "https://api.syncpay.pro")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "troque-isso")
DB = "database.db"

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()

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
        await db.commit()

async def ensure_user(m: Message):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id, username, first_name) VALUES(?,?,?)",
                         (m.from_user.id, m.from_user.username, m.from_user.first_name))
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
                if old > now: base = old
            except Exception: pass
        new_exp = base + datetime.timedelta(minutes=minutes)
        await db.execute("UPDATE users SET expires_at=? WHERE user_id=?", (new_exp.isoformat(), user_id))
        await db.commit()
        return new_exp

async def profile_text(user_id: int):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT trial_used, expires_at FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
    exp = row[1] if row else None
    if not exp:
        return "👤 Meu perfil\n\n⏳ Assinatura: sem tempo ativo"
    exp_dt = datetime.datetime.fromisoformat(exp)
    left = exp_dt - datetime.datetime.utcnow()
    if left.total_seconds() <= 0:
        return "👤 Meu perfil\n\n⚠️ Assinatura expirada"
    return f"👤 Meu perfil\n\n✅ Ativo até: {exp_dt.strftime('%d/%m/%Y %H:%M')} UTC\n⏳ Tempo restante: {str(left).split('.')[0]}"

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 RESGATAR 3 DIAS GRÁTIS", callback_data="trial")],
        [InlineKeyboardButton(text="💎 VER PLANOS", callback_data="planos")],
        [InlineKeyboardButton(text="⚙️ CONFIGURAR LOOP", callback_data="config_loop")],
        [InlineKeyboardButton(text="👤 MEU PERFIL", callback_data="perfil")],

        [InlineKeyboardButton(
            text="📖 COMO CONFIGURAR",
            url="https://t.me/aulasloopgram"
        )],

        [InlineKeyboardButton(
            text="📞 SUPORTE",
            url=f"https://t.me/{SUPORTE_USERNAME}"
        )],
    ])

def planos_kb():
    kb = InlineKeyboardBuilder()
    for pid, p in PLANOS.items():
        kb.button(text=f"{p['nome']} — R${p['valor']}", callback_data=f"comprar:{pid}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="voltar"))
    return kb.as_markup()

@dp.message(Command("start"))
async def start(m: Message):
    await ensure_user(m)
    await m.answer("👋 Olá! Seja bem-vindo(a) ao Loop Mensage!\n\nAutomação inteligente para divulgação no Telegram.\n\n🎁 Você pode testar por 3 dias grátis.", reply_markup=main_kb())

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
        cur = await db.execute("SELECT trial_used FROM users WHERE user_id=?", (c.from_user.id,))
        row = await cur.fetchone()
        if row and row[0] == 1:
            await c.answer("Você já usou o teste grátis.", show_alert=True); return
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
    texto = """
⚙️ CONFIGURAR LOOP MENSAGE

Aqui você configura sua divulgação automática segura.

✅ Permitido:
• Grupos onde você é admin
• Canais onde você é admin
• Mensagens com intervalo alto
• Pausar quando quiser

🚫 Não permitido:
• Enviar em grupos onde você não é admin
• Enviar privado para pessoas
• Spam em massa

Escolha uma opção:
"""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 CONECTAR CONTA TELEGRAM", callback_data="connect_account")],
        [InlineKeyboardButton(text="📢 MEUS GRUPOS/CANAIS", callback_data="my_chats")],
        [InlineKeyboardButton(text="📝 DEFINIR MENSAGEM", callback_data="set_message")],
        [InlineKeyboardButton(text="⏱️ DEFINIR INTERVALO", callback_data="set_interval")],
        [InlineKeyboardButton(text="▶️ INICIAR LOOP", callback_data="start_loop")],
        [InlineKeyboardButton(text="⏹️ PARAR LOOP", callback_data="stop_loop")],
        [InlineKeyboardButton(text="⬅️ VOLTAR", callback_data="voltar")],
    ])

    await c.message.edit_text(texto, reply_markup=kb)


@dp.callback_query(F.data == "connect_account")
async def connect_account(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "🔐 Conectar conta Telegram\n\n"
        "Essa parte será feita com Telethon.\n"
        "Na próxima etapa vamos adicionar login por número + código.\n\n"
        "⚠️ Use apenas em grupos/canais onde você é admin."
    )


@dp.callback_query(F.data == "my_chats")
async def my_chats(c: CallbackQuery):
    await c.answer("Em breve: listar grupos/canais onde você é admin.", show_alert=True)


@dp.callback_query(F.data == "set_message")
async def set_message(c: CallbackQuery):
    await c.answer("Em breve: cadastrar mensagem.", show_alert=True)


@dp.callback_query(F.data == "set_interval")
async def set_interval(c: CallbackQuery):
    await c.answer("Em breve: definir intervalo mínimo seguro.", show_alert=True)


@dp.callback_query(F.data == "start_loop")
async def start_loop(c: CallbackQuery):
    await c.answer("Em breve: iniciar divulgação segura.", show_alert=True)


@dp.callback_query(F.data == "stop_loop")
async def stop_loop(c: CallbackQuery):
    await c.answer("Em breve: parar divulgação.", show_alert=True)

async def syncpay_create_pix(order_id: str, amount: Decimal, user_id: int, desc: str):
    # IMPORTANTE: ajuste os endpoints/campos conforme sua documentação SyncPay.
    # Deixei centralizado para ficar fácil trocar se a SyncPay usar outro formato.
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
    pid = c.data.split(":",1)[1]
    p = PLANOS[pid]
    order_id = str(uuid.uuid4())
    await c.message.answer("⏳ Gerando Pix automático...")
    try:
        sync_id, pix_code = await syncpay_create_pix(order_id, p["valor"], c.from_user.id, f"Loop Mensage - {p['nome']}")
    except Exception as e:
        await c.message.answer(f"❌ Erro ao gerar Pix. Verifique as credenciais/endpoints da SyncPay.\n\nDetalhe: {e}")
        return
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
            (order_id, c.from_user.id, pid, str(p["valor"]), "pending", sync_id, pix_code, datetime.datetime.utcnow().isoformat()))
        await db.commit()
    img = qrcode.make(pix_code)
    import io
    bio = io.BytesIO(); img.save(bio, format="PNG"); bio.seek(0)
    await c.message.answer_photo(BufferedInputFile(bio.read(), filename="pix.png"),
        caption=f"✅ Pix gerado para {p['nome']} — R${p['valor']}\n\nCopia e cola:\n<code>{pix_code}</code>", parse_mode="HTML")

@app.post("/syncpay/webhook")
async def syncpay_webhook(request: Request):
    # Opcional: valide assinatura/cabeçalho se a SyncPay fornecer.
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
        if not row: raise HTTPException(404, "pedido não encontrado")
        user_id, plano_id, old_status = row
        if old_status == "paid": return {"ok": True, "already_paid": True}
        await db.execute("UPDATE orders SET status='paid' WHERE order_id=?", (order_id,))
        await db.commit()
    exp = await add_time(user_id, PLANOS[plano_id]["minutos"])
    await bot.send_message(user_id, f"✅ Pagamento confirmado!\nSeu plano {PLANOS[plano_id]['nome']} foi ativado.\nAtivo até {exp.strftime('%d/%m/%Y %H:%M')} UTC")
    return {"ok": True}

@dp.message(Command("adddias"))
async def adddias(m: Message):
    if m.from_user.id != ADMIN_ID: return
    parts = m.text.split()
    if len(parts) != 3:
        await m.answer("Use: /adddias ID dias"); return
    user_id, dias = int(parts[1]), int(parts[2])
    exp = await add_time(user_id, dias * 24 * 60)
    await m.answer(f"✅ Adicionado {dias} dias para {user_id}. Expira em {exp}")

@dp.message(Command("broadcast"))
async def broadcast(m: Message):
    if m.from_user.id != ADMIN_ID: return
    texto = m.text.replace('/broadcast','',1).strip()
    if not texto: await m.answer("Use: /broadcast mensagem"); return
    enviados = 0
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT user_id FROM users")
        async for (uid,) in cur:
            try:
                await bot.send_message(uid, texto); enviados += 1
            except Exception: pass
    await m.answer(f"✅ Enviado para {enviados} usuários.")

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
