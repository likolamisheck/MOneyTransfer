import os
import math
import asyncio
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import httpx
from aiohttp import web

# Load env
ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_URL = os.getenv("GOOGLE_SHEET_CSV_URL") or os.getenv("GOOGLE_SHEET_URL")
BASE_URL = os.getenv("BASE_URL")

if not TOKEN or not SHEET_URL or not BASE_URL:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_URL, or BASE_URL in .env")

TIMEOUT = 10.0

# Fee table
FEE_BRACKETS = [
    (100,450,25),(500,1500,50),(1600,3400,100),(3500,6400,150),
    (6500,10000,325),(10001,15000,500),(15001,20000,700),(20001,40000,1000)
]

def fee_for_kw(amount_k: float):
    for lo, hi, fee in FEE_BRACKETS:
        if lo <= amount_k <= hi:
            return fee, (lo, hi)
    return None, None

def fmt_money(x, cur=""):
    s = f"{x:,.2f}"
    if s.endswith(".00"): s = s[:-3]
    return f"{s} {cur}".strip()

def parse_amount(text: str) -> float:
    t = text.replace(" ","").replace(",",".")
    return float(t)

# Google Sheet CSV
def derive_csv_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    q = dict(urllib.parse.parse_qsl(parsed.query))
    if "/spreadsheets/d/e/" in path and "/pubhtml" in path:
        path = path.replace("/pubhtml","/pub")
        q["output"]="csv"
        new_q=urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(path=path,query=new_q))
    if "/spreadsheets/d/e/" in path and "/pub" in path:
        q["output"]="csv"
        new_q=urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(query=new_q))
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", path)
    if m:
        sheet_id = m.group(1)
        gid = q.get("gid","0")
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return url

CSV_URL = derive_csv_url(SHEET_URL)

async def fetch_rate_from_sheet():
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(CSV_URL, headers={"Accept":"text/csv, text/plain;q=0.9, */*;q=0.1"})
        r.raise_for_status()
        txt = r.text.strip()
        if "<html" in txt.lower(): raise RuntimeError("Got HTML instead of CSV.")
        m = re.search(r'[-+]?\d+(?:[.,]\d+)?', txt)
        if not m: raise RuntimeError(f"Could not parse numeric rate from: {txt[:120]!r}")
        rub_per_zmw = float(m.group(0).replace(",","."))
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return rub_per_zmw, updated

# UI helpers
def menu_keyboard() -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton("📈 Google rate")],
                  [types.KeyboardButton("💸 Receive Kwacha"),types.KeyboardButton("💶 Receive Rubles")],
                  [types.KeyboardButton("ℹ️ Fees")]],
        resize_keyboard=True, input_field_placeholder="Choose an option…",
        selective=False, is_persistent=True
    )

def header(title:str) -> str: return f"<b>{title}</b>\n"
def line(label:str,value:str,width:int=18) -> str: return f"{label:<{width}} {value}\n"
def calc_block(pairs) -> str: return "<pre>"+"".join(line(lbl,val) for lbl,val in pairs)+"</pre>"

# FSM
class Form(StatesGroup):
    waiting_kw_amount = State()
    waiting_rub_amount = State()

# Bot setup
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# Handlers
@dp.message(Command("start"))
async def start_cmd(m:types.Message,state:FSMContext):
    await state.clear()
    text = header("🚀 MONEY TRANSFER — Quick Menu") + \
           "• 📈 Google rate\n• 💸 Receive Kwacha\n• 💶 Receive Rubles\n• ℹ️ Fees\n"
    await m.answer(text, reply_markup=menu_keyboard())

@dp.message(F.text=="ℹ️ Fees")
async def fees(m:types.Message,state:FSMContext):
    await state.clear()
    lines = ["<b>📋 Fee table (Kwacha)</b>"]
    for lo,hi,fee in FEE_BRACKETS: lines.append(f"{lo:,}–{hi:,} K → <b>{fee:,} K</b>")
    await m.answer("\n".join(lines),reply_markup=menu_keyboard())

@dp.message(F.text=="📈 Google rate")
async def google_rate(m:types.Message,state:FSMContext):
    await state.clear()
    try:
        rub_per_zmw,updated = await fetch_rate_from_sheet()
    except Exception as e:
        print("[rate fetch error]",repr(e))
        return await m.answer("Sorry, could not fetch rate.",reply_markup=menu_keyboard())
    zmw_per_rub = 1.0/rub_per_zmw if rub_per_zmw else math.inf
    txt = header("📈 Current Google rate")+calc_block([
        ("1 ZMW → RUB",f"{rub_per_zmw:.4f}"),
        ("1 RUB → ZMW",f"{zmw_per_rub:.4f}"),
        ("Updated",updated),
        ("Source","Google Sheet (CSV)")
    ])
    await m.answer(txt,reply_markup=menu_keyboard())

# Webhook
async def handle(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(update)
    return web.Response()

async def on_startup(app):
    await bot.set_webhook(f"{BASE_URL}/{TOKEN}")

async def on_cleanup(app):
    await bot.session.close()

app = web.Application()
app.router.add_post(f"/{TOKEN}",handle)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

web.run_app(app,port=int(os.environ.get("PORT",5000)))
