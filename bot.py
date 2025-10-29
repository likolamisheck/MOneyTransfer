import os, asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
import httpx
import math
import re
import urllib.parse
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")
BASE_URL = os.environ.get("BASE_URL")

if not TOKEN or not SHEET_URL or not BASE_URL:
    raise RuntimeError("One or more required environment variables are missing: TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_URL, BASE_URL")

TIMEOUT = 10.0

FEE_BRACKETS = [
    (100, 450, 25),
    (500, 1500, 50),
    (1600, 3400, 100),
    (3500, 6400, 150),
    (6500, 10000, 325),
    (10001, 15000, 500),
    (15001, 20000, 700),
    (20001, 40000, 1000),
]

MIN_K = FEE_BRACKETS[0][0]
MAX_K = FEE_BRACKETS[-1][1]

def fee_for_kw(amount_k):
    for lo, hi, fee in FEE_BRACKETS:
        if lo <= amount_k <= hi:
            return fee, (lo, hi)
    return None, None

def fmt_money(x, cur=""):
    s = f"{x:,.2f}"
    if s.endswith(".00"):
        s = s[:-3]
    return f"{s} {cur}".strip()

def parse_amount(text: str) -> float:
    t = text.replace(" ", "").replace(",", ".")
    return float(t)

def derive_csv_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    q = dict(urllib.parse.parse_qsl(parsed.query))
    if "/spreadsheets/d/e/" in path:
        if "/pubhtml" in path:
            path = path.replace("/pubhtml", "/pub")
        q["output"] = "csv"
        new_q = urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(path=path, query=new_q))
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", path)
    if m:
        sheet_id = m.group(1)
        gid = q.get("gid", "0")
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return url

CSV_URL = derive_csv_url(SHEET_URL)

async def fetch_rate_from_sheet():
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(CSV_URL)
        r.raise_for_status()
        txt = r.text.strip()
        m = re.search(r'[-+]?\d+(?:[.,]\d+)?', txt)
        if not m:
            raise RuntimeError(f"Could not parse numeric rate from sheet: {txt[:120]!r}")
        rub_per_zmw = float(m.group(0).replace(',', '.'))
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return rub_per_zmw, updated

def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📈 Google rate")],
            [KeyboardButton("💸 Receive Kwacha"), KeyboardButton("💶 Receive Rubles")],
            [KeyboardButton("ℹ️ Fees")]
        ],
        resize_keyboard=True
    )

def header(title: str) -> str:
    return f"<b>{title}</b>\n"

def line(label: str, value: str, width: int = 18) -> str:
    return f"{label:<{width}} {value}\n"

def calc_block(pairs) -> str:
    body = "".join(line(lbl, val) for lbl, val in pairs)
    return f"<pre>{body}</pre>"

class Form(StatesGroup):
    waiting_kw_amount = State()
    waiting_rub_amount = State()

bot = Bot(TOKEN, parse_mode="HTML")
dp = Dispatcher(bot, storage=MemoryStorage())

# ---- Handlers ----
@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    text = header("🚀 MONEY TRANSFER — Quick Menu") + "Use buttons below:\n• 📈 Google rate\n• 💸 Receive Kwacha\n• 💶 Receive Rubles\n• ℹ️ Fees"
    await m.answer(text, reply_markup=menu_keyboard())

@dp.message(F.text == "ℹ️ Fees")
async def fees(m: Message, state: FSMContext):
    await state.clear()
    lines = ["<b>📋 Fee table (Kwacha)</b>"]
    for lo, hi, fee in FEE_BRACKETS:
        lines.append(f"{lo:,}–{hi:,} K  →  <b>{fee:,} K</b>")
    await m.answer("\n".join(lines), reply_markup=menu_keyboard())

@dp.message(F.text == "📈 Google rate")
async def google_rate(m: Message, state: FSMContext):
    await state.clear()
    try:
        rub_per_zmw, updated = await fetch_rate_from_sheet()
    except Exception:
        return await m.answer("Sorry, could not fetch rate now.", reply_markup=menu_keyboard())
    zmw_per_rub = 1.0 / rub_per_zmw
    txt = header("📈 Current Google rate") + calc_block([("1 ZMW → RUB", f"{rub_per_zmw:.4f}"), ("1 RUB → ZMW", f"{zmw_per_rub:.4f}"), ("Updated", updated)])
    await m.answer(txt, reply_markup=menu_keyboard())

# ---- Webhook setup ----
from aiohttp import web
from aiogram.types import Update

async def handle(request):
    data = await request.json()
    update = Update.to_object(data)
    await dp.feed_update(update)
    return web.Response()

async def on_startup(app):
    await bot.set_webhook(f"{BASE_URL}/{TOKEN}")

async def on_cleanup(app):
    await bot.session.close()

app = web.Application()
app.router.add_post(f"/{TOKEN}", handle)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

web.run_app(app, port=int(os.environ.get("PORT", 5000)))
