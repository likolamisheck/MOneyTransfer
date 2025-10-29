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
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import httpx
from aiohttp import web

# ---- Load env from the script's folder ----
ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_URL = os.getenv("GOOGLE_SHEET_CSV_URL") or os.getenv("GOOGLE_SHEET_URL")
if not TOKEN:
    raise RuntimeError(f"TELEGRAM_BOT_TOKEN is missing. Expected in {ENV_PATH}")
if not SHEET_URL:
    raise RuntimeError(f"GOOGLE_SHEET_URL (or GOOGLE_SHEET_CSV_URL) is missing in {ENV_PATH}")

TIMEOUT = 10.0

# -------------------- Fee table (Kwacha) --------------------
FEE_BRACKETS = [
    (100,    450,    25),
    (500,    1500,   50),
    (1600,   3400,   100),
    (3500,   6400,   150),
    (6500,   10000,  325),
    (10001,  15000,  500),
    (15001,  20000,  700),
    (20001,  40000,  1000),
]

MIN_K = FEE_BRACKETS[0][0]
MAX_K = FEE_BRACKETS[-1][1]

def fee_for_kw(amount_k: float):
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

# -------------------- Google Sheet rate (ZMW->RUB) --------------------
def derive_csv_url(url: str) -> str:
    if "output=csv" in url:
        return url
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    q = dict(urllib.parse.parse_qsl(parsed.query))
    if "/spreadsheets/d/e/" in path and "/pubhtml" in path:
        path = path.replace("/pubhtml", "/pub")
        q["output"] = "csv"
        new_q = urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(path=path, query=new_q))
    if "/spreadsheets/d/e/" in path and "/pub" in path:
        q["output"] = "csv"
        new_q = urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(query=new_q))
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", path)
    if m:
        sheet_id = m.group(1)
        gid = q.get("gid", "0")
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return url

CSV_URL = derive_csv_url(SHEET_URL)

async def fetch_rate_from_sheet():
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(CSV_URL, headers={"Accept": "text/csv, text/plain;q=0.9, */*;q=0.1"})
        r.raise_for_status()
        txt = r.text.strip()
        if "<html" in txt.lower():
            raise RuntimeError("Got HTML instead of CSV. Check publish/share settings.")
        m = re.search(r'[-+]?\d+(?:[.,]\d+)?', txt)
        if not m:
            raise RuntimeError(f"Could not parse numeric rate from: {txt[:120]!r}")
        rub_per_zmw = float(m.group(0).replace(',', '.'))
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return rub_per_zmw, updated

# -------------------- UI helpers --------------------
def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Google rate")],
            [KeyboardButton(text="💸 Receive Kwacha"), KeyboardButton(text="💶 Receive Rubles")],
            [KeyboardButton(text="ℹ️ Fees")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Choose an option…",
        selective=False,
        is_persistent=True,
    )

def header(title: str) -> str:
    return f"<b>{title}</b>\n"

def line(label: str, value: str, width: int = 18) -> str:
    return f"{label:<{width}} {value}\n"

def calc_block(pairs) -> str:
    body = "".join(line(lbl, val) for lbl, val in pairs)
    return f"<pre>{body}</pre>"

# -------------------- FSM --------------------
class Form(StatesGroup):
    waiting_kw_amount = State()
    waiting_rub_amount = State()

# -------------------- Bot setup --------------------
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# -------------------- Handlers --------------------
# (You can copy your existing handlers here)
# Example: start command
@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    text = (
        header("🚀 MONEY TRANSFER — Quick Menu") +
        "Use the buttons below anytime:\n"
        "• 📈 Google rate — show ZMW↔RUB\n"
        "• 💸 Receive Kwacha — enter K to pay out (we add fee & show RUB to send)\n"
        "• 💶 Receive Rubles — enter RUB to pay out (we add fee in K)\n"
        "• ℹ️ Fees — see fee brackets\n"
    )
    await m.answer(text, reply_markup=menu_keyboard())

# -------------------- Webhook for Render --------------------
async def handle_webhook(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return web.Response(text="ok")

app = web.Application()
app.router.add_post("/webhook", handle_webhook)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"Starting webhook bot on port {port}")
    web.run_app(app, port=port)
