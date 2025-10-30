import os
import math
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

# Load env
ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_URL = os.getenv("GOOGLE_SHEET_CSV_URL") or os.getenv("GOOGLE_SHEET_URL")
BASE_URL = os.getenv("BASE_URL")

if not TOKEN or not SHEET_URL or not BASE_URL:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN, GOOGLE_SHEET_URL, or BASE_URL in environment")

TIMEOUT = 10.0

AGENT_PHONE = "+79938916814"

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
        r = await client.get(CSV_URL, headers={"Accept": "text/csv"})
        r.raise_for_status()
        txt = r.text.strip()
        m = re.search(r'[-+]?\d+(?:[.,]\d+)?', txt)
        if not m:
            raise RuntimeError(f"Could not parse rate from: {txt[:80]}")
        rub_per_zmw = float(m.group(0).replace(',', '.'))
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        return rub_per_zmw, updated

# -------------------- UI --------------------
def menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📈 Google rate")],
            [KeyboardButton(text="💸 Receive Kwacha"), KeyboardButton(text="💶 Receive Rubles")],
            [KeyboardButton(text="ℹ️ Fees")],
        ],
        resize_keyboard=True
    )

def header(title): return f"<b>{title}</b>\n"

def line(label, value, width=18): return f"{label:<{width}} {value}\n"

def calc_block(pairs): return f"<pre>{''.join(line(k,v) for k,v in pairs)}</pre>"

# -------------------- FSM --------------------
class Form(StatesGroup):
    waiting_kw_amount = State()
    waiting_rub_amount = State()

# -------------------- BOT --------------------
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

@dp.message(Command("start"))
async def start_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        header("🚀 MONEY TRANSFER — Quick Menu") +
        "Use the buttons below anytime:\n"
        "• 📈 Google rate — show ZMW↔RUB\n"
        "• 💸 Receive Kwacha — enter K to pay out (we add fee & show RUB to send)\n"
        "• 💶 Receive Rubles — enter RUB to pay out (we add fee in K)\n"
        "• ℹ️ Fees — see fee brackets",
        reply_markup=menu_keyboard()
    )

@dp.message(F.text == "ℹ️ Fees")
async def fees(m: Message, state: FSMContext):
    await state.clear()
    lines = ["<b>📋 Fee table (Kwacha)</b>"]
    for lo, hi, fee in FEE_BRACKETS:
        lines.append(f"{lo:,}–{hi:,} K → <b>{fee:,} K</b>")
    await m.answer("\n".join(lines), reply_markup=menu_keyboard())

@dp.message(F.text == "📈 Google rate")
async def google_rate(m: Message, state: FSMContext):
    await state.clear()
    rub_per_zmw, updated = await fetch_rate_from_sheet()
    zmw_per_rub = 1 / rub_per_zmw if rub_per_zmw else math.inf
    txt = header("📈 Current Google rate") + calc_block([
        ("1 ZMW → RUB", f"{rub_per_zmw:.4f}"),
        ("1 RUB → ZMW", f"{zmw_per_rub:.4f}"),
        ("Updated", updated),
        ("Source", "Google Sheet"),
    ])
    await m.answer(txt, reply_markup=menu_keyboard())

# 💸 Receive Kwacha
@dp.message(F.text == "💸 Receive Kwacha")
async def choose_kw(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_kw_amount)
    await m.answer(
        header("💸 Receive Kwacha") +
        f"Enter the Kwacha amount the recipient should get ({MIN_K}–{MAX_K} K).",
        reply_markup=menu_keyboard()
    )

@dp.message(Form.waiting_kw_amount)
async def handle_kw(m: Message, state: FSMContext):
    try:
        want_k = parse_amount(m.text)
    except:
        return await m.answer("Please enter a valid number, e.g. 6500.")
    if want_k < MIN_K or want_k > MAX_K:
        return await m.answer(f"Amount {fmt_money(want_k,'K')} is outside supported range ({MIN_K}-{MAX_K} K).")

    fee_k, (lo, hi) = fee_for_kw(want_k)
    rub_per_zmw, updated = await fetch_rate_from_sheet()
    total_k = want_k + fee_k
    rub_to_send = total_k * rub_per_zmw

    message_text = (
        f"CLIENT wants to receive {fmt_money(want_k,'ZMW')}.\n\n"
        f"Transfer fee: {fmt_money(fee_k,'ZMW')} (because your amount is in the {lo:,}–{hi:,} range)\n"
        f"Total to send: {fmt_money(total_k,'ZMW')} including the fee\n"
        f"Exchange rate used: 1 ZMW = {rub_per_zmw:.4f} RUB\n"
        f"Equivalent in Rubles: {fmt_money(rub_to_send,'RUB')}\n"
        f"Updated: {updated}\n\n"
        f"Click below to open WhatsApp to initiate the transaction 👇"
    )

    encoded = urllib.parse.quote(message_text)
    wa_link = f"https://wa.me/{AGENT_PHONE.replace('+','')}?text={encoded}"

    await state.clear()
    await m.answer(message_text + f"\n\n👉 <a href='{wa_link}'>Contact agent on WhatsApp</a>", reply_markup=menu_keyboard())

# 💶 Receive Rubles
@dp.message(F.text == "💶 Receive Rubles")
async def choose_rub(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_rub_amount)
    await m.answer(header("💶 Receive Rubles") + "Enter the Ruble amount the client should get, e.g. 10000.")

@dp.message(Form.waiting_rub_amount)
async def handle_rub(m: Message, state: FSMContext):
    try:
        want_rub = parse_amount(m.text)
    except:
        return await m.answer("Please enter a valid number, e.g. 10000.")
    rub_per_zmw, updated = await fetch_rate_from_sheet()
    zmw_per_rub = 1 / rub_per_zmw
    base_k = want_rub * zmw_per_rub
    if base_k < MIN_K or base_k > MAX_K:
        return await m.answer(f"The equivalent {fmt_money(base_k,'K')} is outside supported fee range.")

    fee_k, (lo, hi) = fee_for_kw(base_k)
    total_k = base_k + fee_k

    message_text = (
        f"CLIENT wants to receive {fmt_money(want_rub,'RUB')}.\n\n"
        f"Equivalent in Kwacha: {fmt_money(base_k,'ZMW')}\n"
        f"Transfer fee: {fmt_money(fee_k,'ZMW')} (because your amount is in the {lo:,}–{hi:,} range)\n"
        f"Total to send: {fmt_money(total_k,'ZMW')} including the fee\n"
        f"Exchange rate used: 1 ZMW = {rub_per_zmw:.4f} RUB\n"
        f"Updated: {updated}\n\n"
        f"Click below to open WhatsApp to initiate the transaction 👇"
    )

    encoded = urllib.parse.quote(message_text)
    wa_link = f"https://wa.me/{AGENT_PHONE.replace('+','')}?text={encoded}"

    await state.clear()
    await m.answer(message_text + f"\n\n👉 <a href='{wa_link}'>Contact agent on WhatsApp</a>", reply_markup=menu_keyboard())

# -------------------- Webhook --------------------
async def handle(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.feed_update(bot=bot, update=update)
    return web.Response()

async def on_startup(app):
    await bot.set_webhook(f"{BASE_URL}/{TOKEN}")

async def on_cleanup(app):
    await bot.session.close()

app = web.Application()
app.router.add_post(f"/{TOKEN}", handle)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, port=int(os.environ.get("PORT", 5000)))
