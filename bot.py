import os, math, asyncio, re, urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, Update
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import httpx
from aiohttp import web

# ---- Load .env ----
ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_URL = os.getenv("GOOGLE_SHEET_URL")
BASE_URL = os.getenv("BASE_URL")  # e.g., https://moneytransfer-0wvi.onrender.com

if not TOKEN or not SHEET_URL or not BASE_URL:
    raise RuntimeError("Make sure TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_URL, BASE_URL are set in .env")

# ---- Fee table ----
FEE_BRACKETS = [
    (100, 450, 25), (500, 1500, 50), (1600, 3400, 100),
    (3500, 6400, 150), (6500, 10000, 325), (10001, 15000, 500),
    (15001, 20000, 700), (20001, 40000, 1000),
]
MIN_K, MAX_K = FEE_BRACKETS[0][0], FEE_BRACKETS[-1][1]

def fee_for_kw(amount_k): 
    for lo, hi, fee in FEE_BRACKETS:
        if lo <= amount_k <= hi: return fee, (lo, hi)
    return None, None

def fmt_money(x, cur=""): 
    s = f"{x:,.2f}".rstrip("0").rstrip(".") if "." in f"{x:,.2f}" else f"{x:,.2f}"
    return f"{s} {cur}".strip()

def parse_amount(text): return float(text.replace(" ", "").replace(",", "."))

def derive_csv_url(url):
    if "output=csv" in url: return url
    parsed = urllib.parse.urlparse(url)
    path, q = parsed.path, dict(urllib.parse.parse_qsl(parsed.query))
    if "/spreadsheets/d/e/" in path and "/pubhtml" in path:
        path = path.replace("/pubhtml", "/pub")
        q["output"] = "csv"
        return urllib.parse.urlunparse(parsed._replace(path=path, query=urllib.parse.urlencode(q)))
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", path)
    if m: return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={q.get('gid','0')}"
    return url

CSV_URL = derive_csv_url(SHEET_URL)
TIMEOUT = 10.0

async def fetch_rate_from_sheet():
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(CSV_URL)
        r.raise_for_status()
        txt = r.text.strip()
        m = re.search(r'[-+]?\d+(?:[.,]\d+)?', txt)
        rub_per_zmw = float(m.group(0).replace(",", ".")) if m else None
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return rub_per_zmw, updated

def menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📈 Google rate")],
            [KeyboardButton("💸 Receive Kwacha"), KeyboardButton("💶 Receive Rubles")],
            [KeyboardButton("ℹ️ Fees")]
        ], resize_keyboard=True, input_field_placeholder="Choose an option…"
    )

def header(title): return f"<b>{title}</b>\n"
def line(label, value, width=18): return f"{label:<{width}} {value}\n"
def calc_block(pairs): return f"<pre>{''.join(line(lbl,val) for lbl,val in pairs)}</pre>"

class Form(StatesGroup):
    waiting_kw_amount = State()
    waiting_rub_amount = State()

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ---- Handlers ----
@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(header("🚀 MONEY TRANSFER — Quick Menu") +
                   "Use the buttons below anytime:\n• 📈 Google rate\n• 💸 Receive Kwacha\n• 💶 Receive Rubles\n• ℹ️ Fees",
                   reply_markup=menu_keyboard())

@dp.message(F.text=="ℹ️ Fees")
async def fees(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("<b>📋 Fee table (Kwacha)</b>\n" + "\n".join(f"{lo:,}–{hi:,} K → <b>{fee:,} K</b>" for lo,hi,fee in FEE_BRACKETS),
                   reply_markup=menu_keyboard())

@dp.message(F.text=="📈 Google rate")
async def google_rate(m: Message, state: FSMContext):
    await state.clear()
    try: rub_per_zmw, updated = await fetch_rate_from_sheet()
    except: return await m.answer("Could not fetch Google rate.", reply_markup=menu_keyboard())
    await m.answer(header("📈 Current Google rate") + calc_block([
        ("1 ZMW → RUB", f"{rub_per_zmw:.4f}"), ("1 RUB → ZMW", f"{1/rub_per_zmw:.4f}"), 
        ("Updated", updated), ("Source", "Google Sheet (CSV)")
    ]), reply_markup=menu_keyboard())

@dp.message(F.text=="💸 Receive Kwacha")
async def choose_kw(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_kw_amount)
    await m.answer(header("💸 Receive Kwacha") + f"Enter Kwacha amount ({MIN_K}-{MAX_K} K), e.g. <code>6500</code>.",
                   reply_markup=menu_keyboard())

@dp.message(Form.waiting_kw_amount)
async def handle_kw_amount(m: Message, state: FSMContext):
    try: want_k=parse_amount(m.text)
    except: return await m.answer("Enter a valid number.", reply_markup=menu_keyboard())
    if want_k<MIN_K or want_k>MAX_K: return await m.answer(f"{want_k} K outside range ({MIN_K}-{MAX_K})", reply_markup=menu_keyboard())
    fee_k,bracket=fee_for_kw(want_k)
    rub_per_zmw,_=await fetch_rate_from_sheet()
    total_k=want_k+fee_k
    rub_to_send=total_k*rub_per_zmw
    await state.clear()
    await m.answer(header("✅ Quote — RUB to send") + calc_block([
        ("Recipient gets", fmt_money(want_k,"K")), ("Fee", f"{fmt_money(fee_k,'K')} (bracket {bracket[0]}–{bracket[1]} K)"),
        ("Total basis", fmt_money(total_k,"K")), ("Rate used", f"1 ZMW = {rub_per_zmw:.4f} RUB"), ("You send", fmt_money(rub_to_send,"RUB"))
    ]), reply_markup=menu_keyboard())

# ---- Webhook server ----
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

