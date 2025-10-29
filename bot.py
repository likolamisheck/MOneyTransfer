import os, math, asyncio, re, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import httpx

# ---- Load env from the script's folder (robust even if CWD differs) ----
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
# FROM_K, TO_K, FEE_K  — inclusive ranges
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
    # published HTML -> CSV
    if "/spreadsheets/d/e/" in path and "/pubhtml" in path:
        path = path.replace("/pubhtml", "/pub")
        q["output"] = "csv"
        new_q = urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(path=path, query=new_q))
    # published -> ensure csv
    if "/spreadsheets/d/e/" in path and "/pub" in path:
        q["output"] = "csv"
        new_q = urllib.parse.urlencode(q)
        return urllib.parse.urlunparse(parsed._replace(query=new_q))
    # normal share link -> export csv
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", path)
    if m:
        sheet_id = m.group(1)
        gid = q.get("gid", "0")
        return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
    return url

CSV_URL = derive_csv_url(SHEET_URL)

async def fetch_rate_from_sheet():
    """Return (rub_per_zmw, updated_text). A1 in the sheet must contain a single number."""
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

# -------------------- Pretty UI helpers --------------------
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
dp  = Dispatcher(storage=MemoryStorage())

# -------------------- Handlers --------------------
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
    except Exception as e:
        print("[rate fetch error]", repr(e))
        return await m.answer("Sorry, I couldn’t fetch the Google rate right now. Try again shortly.",
                              reply_markup=menu_keyboard())
    zmw_per_rub = (1.0 / rub_per_zmw) if rub_per_zmw else math.inf
    txt = (
        header("📈 Current Google rate") +
        calc_block([
            ("1 ZMW → RUB", f"{rub_per_zmw:.4f}"),
            ("1 RUB → ZMW", f"{zmw_per_rub:.4f}"),
            ("Updated",      updated),
            ("Source",       "Google Sheet (CSV)"),
        ])
    )
    await m.answer(txt, reply_markup=menu_keyboard())

@dp.message(F.text == "💸 Receive Kwacha")
async def choose_kw(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_kw_amount)
    txt = (
        header("💸 Receive Kwacha") +
        "Enter the Kwacha amount the recipient should get "
        f"(supported {MIN_K}–{MAX_K} K), e.g. <code>6500</code>."
    )
    await m.answer(txt, reply_markup=menu_keyboard())

@dp.message(Form.waiting_kw_amount)
async def handle_kw_amount(m: Message, state: FSMContext):
    try:
        want_k = parse_amount(m.text)
    except ValueError:
        return await m.answer("Please enter a number, e.g. <code>6500</code>.",
                              reply_markup=menu_keyboard())

    if want_k < MIN_K or want_k > MAX_K:
        return await m.answer(
            f"Amount {fmt_money(want_k,'K')} is outside supported fee ranges ({MIN_K}–{MAX_K} K).",
            reply_markup=menu_keyboard()
        )

    fee_k, bracket = fee_for_kw(want_k)
    if fee_k is None:
        return await m.answer("No matching fee bracket for that amount.", reply_markup=menu_keyboard())

    try:
        rub_per_zmw, updated = await fetch_rate_from_sheet()
    except Exception as e:
        print("[rate fetch error]", repr(e))
        return await m.answer("Sorry, I couldn’t fetch the Google rate right now.", reply_markup=menu_keyboard())

    total_k = want_k + fee_k
    rub_to_send = total_k * rub_per_zmw
    lo, hi = bracket

    txt = (
        header("✅ Quote — RUB to send (K payout)") +
        calc_block([
            ("Recipient gets",  fmt_money(want_k, "K")),
            ("Fee",             f"{fmt_money(fee_k,'K')}  (bracket {lo:,}–{hi:,} K)"),
            ("Total basis",     fmt_money(total_k, "K")),
            ("Rate used",       f"1 ZMW = {rub_per_zmw:.4f} RUB (Google)"),
            ("You send",        fmt_money(rub_to_send, "RUB")),
            ("Updated",         updated),
        ]) +
        "Use the buttons below for another quote."
    )
    await state.clear()
    await m.answer(txt, reply_markup=menu_keyboard())

@dp.message(F.text == "💶 Receive Rubles")
async def choose_rub(m: Message, state: FSMContext):
    await state.set_state(Form.waiting_rub_amount)
    txt = (
        header("💶 Receive Rubles") +
        "Enter the Ruble amount the recipient should get, e.g. <code>10000</code>."
    )
    await m.answer(txt, reply_markup=menu_keyboard())

@dp.message(Form.waiting_rub_amount)
async def handle_rub_amount(m: Message, state: FSMContext):
    try:
        want_rub = parse_amount(m.text)
    except ValueError:
        return await m.answer("Please enter a number, e.g. <code>10000</code>.",
                              reply_markup=menu_keyboard())

    try:
        rub_per_zmw, updated = await fetch_rate_from_sheet()
    except Exception as e:
        print("[rate fetch error]", repr(e))
        return await m.answer("Sorry, I couldn’t fetch the Google rate right now.", reply_markup=menu_keyboard())

    zmw_per_rub = 1.0 / rub_per_zmw if rub_per_zmw else math.inf
    base_k = want_rub * zmw_per_rub

    if base_k < MIN_K or base_k > MAX_K:
        return await m.answer(
            f"The Kwacha equivalent ({fmt_money(base_k,'K')}) is outside supported fee ranges "
            f"({MIN_K}–{MAX_K} K). Adjust amount.",
            reply_markup=menu_keyboard()
        )

    fee_k, bracket = fee_for_kw(base_k)
    if fee_k is None:
        return await m.answer("No matching fee bracket for that amount.", reply_markup=menu_keyboard())

    total_k_to_send = base_k + fee_k
    lo, hi = bracket

    txt = (
        header("✅ Quote — K to send (RUB payout)") +
        calc_block([
            ("Recipient gets",     fmt_money(want_rub, "RUB")),
            ("Base K needed",      fmt_money(base_k, "K")),
            ("Fee",                f"{fmt_money(fee_k,'K')}  (bracket {lo:,}–{hi:,} K)"),
            ("You send (K)",       fmt_money(total_k_to_send, "K")),
            ("Rate used",          f"1 ZMW = {rub_per_zmw:.4f} RUB (Google)"),
            ("Updated",            updated),
        ]) +
        "Use the buttons below for another quote."
    )
    await state.clear()
    await m.answer(txt, reply_markup=menu_keyboard())

# Slash commands mirror buttons (optional)
@dp.message(Command("rate"))
async def cmd_rate(m: Message, state: FSMContext):
    return await google_rate(m, state)

@dp.message(Command("fees"))
async def cmd_fees(m: Message, state: FSMContext):
    return await fees(m, state)

@dp.message(Command("debug_link"))
async def cmd_debug_link(m: Message):
    return await m.answer(f"CSV URL:\n{CSV_URL}")

# ---- run ----
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
